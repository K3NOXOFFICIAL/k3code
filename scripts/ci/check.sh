#!/usr/bin/env bash
# Local CI for k3code: runs on your machine what the GitHub Actions workflows ci.yml and gitleaks.yml used to run, so
# no CI minutes are spent. Areas:
#   core     ruff check + ruff format --check (core/ and scripts/), pytest
#   tui      npm ci, build:ink, build, typecheck, eslint, prettier --check, vitest (minus tui/.ci-test-excludes)
#   panes    go build ./cmd/k3, go vet and go test on the packages k3 depends on (never `go test ./...`)
#   vendor   scripts/vendor_check.py (license bookkeeping)
#   secrets  gitleaks on the commits this branch adds over origin/Main, plus uncommitted and staged changes
#   shell    shellcheck and a syntax check (sh -n / bash -n) of install/*.sh, scripts/**/*.sh and .githooks/*
#
# Usage: scripts/ci/check.sh [--quick] [--only AREA]... [--changed] [--post] [--plan]
#   (no option)   the full check, every area
#   --quick       lint, format and the fast checks only (no test suites, no builds)
#   --only AREA   run only AREA (repeatable, or comma separated)
#   --changed     only the areas touched by this branch (vs origin/Main) and by uncommitted changes
#   --post        after a full run on a clean tree whose HEAD is on origin, set the GitHub commit status `local-ci`
#                 on HEAD (success or failure); refused for a partial run, a dirty tree or a commit not on origin
#   --plan        print the areas and commands that would run, run nothing
#   --base REF    compare with REF instead of origin/Main (--changed, secrets)
#
# Logs go to .k3dev/ci/<timestamp>/ (or $K3CODE_CI_LOG_DIR). Runs under `nice -n 10`. Exit status: 0 all areas
# passed, 1 an area failed, 2 usage or precondition error.
set -euo pipefail

if [ "${K3CODE_CI_NICED:-}" != 1 ]; then
  export K3CODE_CI_NICED=1
  exec nice -n 10 bash "$0" "$@"
fi

HERE=$(cd "$(dirname -- "$0")" && pwd)
# The tree to check: the checkout this script is in, or $K3CODE_CI_ROOT (merge-pr.sh checks a merge whose tree may
# predate this script).
ROOT=$(cd "${K3CODE_CI_ROOT:-$HERE/../..}" && pwd)
K3CI_NAME=check.sh
# shellcheck source=scripts/ci/lib.sh
. "$HERE/lib.sh"
strip_git_env
cd "$ROOT"

ALL_AREAS="shell vendor secrets core panes tui"
QUICK=0
CHANGED=0
POST=0
PLAN=0
ONLY=""
BASE=origin/Main

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

valid_area() {
  case " $ALL_AREAS " in *" $1 "*) return 0 ;; esac
  return 1
}

while [ $# -gt 0 ]; do
  case $1 in
    --quick) QUICK=1 ;;
    --changed) CHANGED=1 ;;
    --post) POST=1 ;;
    --plan) PLAN=1 ;;
    --only)
      [ $# -ge 2 ] || die "--only needs an area ($ALL_AREAS)"
      for a in ${2//,/ }; do
        valid_area "$a" || die "unknown area: $a (areas: $ALL_AREAS)"
        ONLY="$ONLY $a"
      done
      shift
      ;;
    --base)
      [ $# -ge 2 ] || die "--base needs a git ref"
      BASE=$2
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

if [ "$POST" = 1 ]; then
  [ "$QUICK$CHANGED$PLAN" = 000 ] && [ -z "$ONLY" ] ||
    die "--post needs the full check: drop --quick, --only, --changed and --plan"
fi

# changed_areas: the areas the files changed vs BASE (committed, staged, unstaged, untracked) belong to. secrets
# always runs.
changed_files() {
  git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null ||
    die "no $BASE to compare with: run git fetch origin (or pass --base REF)"
  {
    git diff --name-only "$BASE...HEAD"
    git diff --name-only HEAD
    git ls-files --others --exclude-standard
  } | sort -u
}

changed_areas() {
  local f areas=" secrets "
  add() { case $areas in *" $1 "*) ;; *) areas="$areas$1 " ;; esac; }
  while IFS= read -r f; do
    case $f in
      core/* | install/*) add core ;;
      scripts/ci/* | .githooks/*) add core && add shell ;;
      tui/*) add tui && add vendor ;;
      panes/*) add panes && add vendor ;;
      VENDOR.toml | NOTICE | LICENSES/* | scripts/vendor_check.py) add vendor ;;
    esac
    case $f in
      scripts/*.py) add core ;;
      *.sh | install/*) add shell ;;
    esac
  done < <(changed_files)
  echo "$areas"
}

# Select the areas, in the fixed order of ALL_AREAS.
selected=""
want=" ${ONLY:-$ALL_AREAS} "
changed=" $ALL_AREAS "
if [ "$CHANGED" = 1 ]; then changed=$(changed_areas) || exit 2; fi
for a in $ALL_AREAS; do
  case "$want" in *" $a "*) ;; *) continue ;; esac
  case "$changed" in *" $a "*) selected="$selected $a" ;; esac
done
selected=${selected# }

MODE=full
[ "$QUICK" = 1 ] && MODE=quick
SHA=$(git rev-parse HEAD)

if [ "$POST" = 1 ]; then
  tree_clean || die "--post refused: the working tree has uncommitted or untracked changes (git status)"
  sha_on_origin "$SHA" || die "--post refused: ${SHA:0:12} is on no origin branch; push it first (git branch -r --contains)"
  have gh || die "--post needs gh: $(install_hint gh)"
fi

if [ "$PLAN" = 0 ]; then
  LOG_DIR=${K3CODE_CI_LOG_DIR:-$ROOT/.k3dev/ci/$(date +%Y%m%dT%H%M%S)}
  mkdir -p "$LOG_DIR"
  LOG_DIR=$(cd "$LOG_DIR" && pwd)
fi

CUR=""
LOG=/dev/null
AREA_OK=1
AREA_NOTE=""

# need TOOL: fail the current area with an install hint when TOOL is missing.
need() {
  [ "$PLAN" = 1 ] && return 0
  [ "$AREA_OK" = 1 ] || return 0
  have "$1" && return 0
  AREA_OK=0
  AREA_NOTE="missing $1"
  printf '[%s] %s is not installed: %s\n' "$CUR" "$1" "$(install_hint "$1")" | tee -a "$LOG" >&2
}

# step NAME DIR CMD...: run CMD in DIR (relative to the repository root), output to the area log. The first failing
# step fails the area and the steps after it are skipped.
step() {
  local name=$1 dir=$2 rc=0
  shift 2
  if [ "$PLAN" = 1 ]; then
    printf '  %-8s %-24s (cd %s && %s)\n' "$CUR" "$name" "$dir" "$*"
    return 0
  fi
  [ "$AREA_OK" = 1 ] || return 0
  printf '[%s] %s\n' "$CUR" "$name"
  printf '\n== %s\n$ (cd %s && %s)\n' "$name" "$dir" "$*" >>"$LOG"
  (cd "$ROOT/$dir" && "$@") >>"$LOG" 2>&1 </dev/null || rc=$?
  if [ "$rc" != 0 ]; then
    AREA_OK=0
    AREA_NOTE="$name failed (exit $rc)"
    printf '[%s] FAILED: %s (exit %s); last lines of %s:\n' "$CUR" "$name" "$rc" "$LOG" >&2
    tail -n 25 "$LOG" | sed 's/^/    /' >&2
  fi
}

full() { [ "$MODE" = full ]; }

# --- areas -------------------------------------------------------------------------------------------------------

area_core() {
  need uv
  if full; then
    need bwrap
    step "bubblewrap works" . bwrap --ro-bind / / --unshare-all true
  fi
  step "ruff check" core uv run ruff check . ../scripts
  step "ruff format --check" core uv run ruff format --check . ../scripts
  if full; then
    # A stalled run must fail instead of hanging the whole check (issue #42). 45 min is far above a normal run;
    # faulthandler_timeout (core/pyproject.toml) prints the stacks of a test stuck for 5 min, so the log says where.
    need timeout
    step "pytest" core timeout --kill-after=30 "${K3CODE_CI_PYTEST_TIMEOUT:-2700}" uv run pytest -q
  fi
}

# npm ci when tui/node_modules does not match package-lock.json (a stamp holds the lock's hash).
tui_deps() {
  local want have_sum=""
  want=$(sha256sum package-lock.json | cut -d' ' -f1)
  [ -f node_modules/.k3ci-lock ] && have_sum=$(cat node_modules/.k3ci-lock)
  if [ -d node_modules ] && [ "$want" = "$have_sum" ]; then
    echo "node_modules matches package-lock.json; npm ci skipped"
    return 0
  fi
  # step runs this under `|| rc=$?`, where errexit is off: stamp only an install that succeeded.
  npm ci --no-audit --no-fund || return $?
  echo "$want" >node_modules/.k3ci-lock
}

# vitest without the known-failing upstream tests in .ci-test-excludes (one glob per line).
tui_vitest() {
  local g args=()
  if [ -f .ci-test-excludes ]; then
    while IFS= read -r g; do [ -z "$g" ] || args+=(--exclude "$g"); done <.ci-test-excludes
  fi
  npx vitest run ${args[@]+"${args[@]}"}
}

area_tui() {
  need node
  need npm
  step "npm ci" tui tui_deps
  if full; then
    step "build:ink" tui npm run build:ink
    step "build" tui npm run build
    step "typecheck" tui npm run typecheck
  fi
  step "eslint" tui npm run lint
  step "prettier --check" tui npx prettier --check .
  if full; then step "vitest" tui tui_vitest; fi
}

area_panes() {
  need go
  if full; then
    need cc
    # -o keeps the binary out of the tree
    step "go build ./cmd/k3" panes go build -o "${LOG_DIR:-<logs>}/k3" ./cmd/k3
  fi
  step "go vet" panes go vet ./internal/k3keys/... ./internal/harness/... ./cmd/k3/
  if full; then
    step "go test -race" panes go test -race ./internal/k3keys/... ./internal/harness/...
    # ./internal/app takes ~4 min alone and several times that when other checks share the machine; the default
    # 10 min package timeout then kills it mid-test (issue #52)
    step "go test input app" panes go test -timeout 30m ./internal/input/ ./internal/app/
  fi
}

area_vendor() {
  need python3
  step "vendor_check" . python3 scripts/vendor_check.py
}

area_secrets() {
  need gitleaks
  if [ "$PLAN" = 0 ] && [ "$AREA_OK" = 1 ] && ! git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null; then
    AREA_OK=0
    AREA_NOTE="no $BASE"
    printf '[%s] no %s to compare with: run git fetch origin (or pass --base REF)\n' "$CUR" "$BASE" | tee -a "$LOG" >&2
  fi
  local opts=(--source . --config .gitleaks.toml --redact --no-banner --exit-code 1)
  step "commits $BASE..HEAD" . gitleaks detect "${opts[@]}" --log-opts="$BASE..HEAD"
  step "unstaged changes" . gitleaks protect "${opts[@]}"
  step "staged changes" . gitleaks protect --staged "${opts[@]}"
}

shell_files() {
  find install scripts .githooks -type f \( -name '*.sh' -o -path '.githooks/*' \) 2>/dev/null | sort
}

# sh -n for #!/bin/sh scripts, bash -n for the rest.
shell_syntax() {
  local f rc=0
  while IFS= read -r f; do
    case $(head -n 1 "$f") in
      '#!/bin/sh'*) sh -n "$f" || rc=1 ;;
      *) bash -n "$f" || rc=1 ;;
    esac
  done < <(shell_files)
  return "$rc"
}

shell_lint() {
  local files=()
  while IFS= read -r f; do files+=("$f"); done < <(shell_files)
  printf '%s\n' "${files[@]}"
  shellcheck -x "${files[@]}"
}

area_shell() {
  need shellcheck
  step "shellcheck" . shell_lint
  step "sh -n / bash -n" . shell_syntax
}

# --- run ---------------------------------------------------------------------------------------------------------

if [ "$PLAN" = 1 ]; then
  echo "mode: $MODE; areas: ${selected:-none}"
  for a in $selected; do
    CUR=$a
    "area_$a"
  done
  exit 0
fi

[ -n "$selected" ] || {
  echo "check.sh: nothing to check"
  exit 0
}

echo "check.sh: $MODE check of ${SHA:0:12}: $selected (logs: $LOG_DIR)"
T0=$(date +%s)
RESULTS=()
FAILED=""
for a in $selected; do
  CUR=$a
  LOG="$LOG_DIR/$a.log"
  : >"$LOG"
  AREA_OK=1
  AREA_NOTE=""
  t=$(date +%s)
  "area_$a"
  d=$(($(date +%s) - t))
  if [ "$AREA_OK" = 1 ]; then
    RESULTS+=("$a|pass|$(fmt_secs "$d")|")
  else
    RESULTS+=("$a|FAIL|$(fmt_secs "$d")|$AREA_NOTE")
    FAILED="$FAILED $a"
  fi
done
TOTAL=$(fmt_secs $(($(date +%s) - T0)))

{
  printf '\n%-8s  %-6s  %-8s  %s\n' area result time "log / note"
  for r in "${RESULTS[@]}"; do
    IFS='|' read -r a res d n <<<"$r"
    printf '%-8s  %-6s  %-8s  %s\n' "$a" "$res" "$d" "${n:+$n; }${LOG_DIR#"$ROOT"/}/$a.log"
  done
  if [ -z "$FAILED" ]; then
    printf '\nlocal-ci: PASS (%s check of %s, %s)\n' "$MODE" "${SHA:0:12}" "$TOTAL"
  else
    printf '\nlocal-ci: FAIL:%s (%s check of %s, %s)\n' "$FAILED" "$MODE" "${SHA:0:12}" "$TOTAL"
  fi
} | tee "$LOG_DIR/summary.txt"

if [ "$POST" = 1 ]; then
  # Re-check: a step that rewrote a tracked file must not get a green status for the commit.
  [ "$(git rev-parse HEAD)" = "$SHA" ] || die "--post refused: HEAD moved during the run"
  tree_clean || die "--post refused: the run left the tree dirty (git status); fix the step that writes to it"
  platform="$(uname -s | tr '[:upper:]' '[:lower:]')/$(uname -m)"
  if [ -z "$FAILED" ]; then
    post_status "$SHA" success "${selected// /,} passed on $platform in $TOTAL"
  else
    failed=${FAILED# }
    post_status "$SHA" failure "failed: ${failed// /,} on $platform in $TOTAL"
  fi
fi

[ -z "$FAILED" ]
