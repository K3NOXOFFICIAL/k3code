# shellcheck shell=bash
# Helpers shared by scripts/ci/check.sh, scripts/ci/merge-pr.sh and scripts/release/release.sh. Source it; it runs
# nothing on its own.

# The commit status that branch protection on Main requires (it replaces the old GitHub Actions checks).
K3CI_CONTEXT=local-ci

# The areas of check.sh, in the order it runs them.
K3CI_AREAS="shell vendor secrets core panes tui"

# merge-pr.sh runs the full check for a pull request above either size (changed files; added plus deleted lines).
K3CI_FULL_FILES=${K3CODE_CI_FULL_FILES:-40}
K3CI_FULL_LINES=${K3CODE_CI_FULL_LINES:-1500}

die() {
  printf '%s: %s\n' "${K3CI_NAME:-k3code-ci}" "$*" >&2
  exit 2
}

note() {
  printf '%s: %s\n' "${K3CI_NAME:-k3code-ci}" "$*" >&2
}

have() {
  command -v "$1" >/dev/null 2>&1
}

# install_hint TOOL: one line on how to get TOOL.
install_hint() {
  case $1 in
    uv) echo "install uv: curl -LsSf https://astral.sh/uv/install.sh | sh (or see https://docs.astral.sh/uv/)" ;;
    node | npm | npx) echo "install Node 22+ with npm: https://nodejs.org/ (or dnf install nodejs / apt install nodejs npm / brew install node)" ;;
    go) echo "install Go (the version in panes/go.mod): https://go.dev/dl/ (or dnf install golang / brew install go)" ;;
    gitleaks) echo "install gitleaks 8.30.1 or newer: https://github.com/gitleaks/gitleaks/releases (or brew install gitleaks)" ;;
    shellcheck) echo "install shellcheck: dnf install ShellCheck / apt install shellcheck / brew install shellcheck" ;;
    timeout) echo "install GNU coreutils (it ships timeout): dnf install coreutils / apt install coreutils" ;;
    bwrap) echo "install bubblewrap (the sandbox the core tests run under): dnf install bubblewrap / apt install bubblewrap" ;;
    gh) echo "install the GitHub CLI and sign in: https://cli.github.com/ then gh auth login" ;;
    python3) echo "install Python 3.12+ (python3 on PATH)" ;;
    cc | gcc) echo "install a C compiler for go test -race: dnf install gcc / apt install gcc / xcode-select --install" ;;
    *) echo "install $1 and put it on PATH" ;;
  esac
}

# repo_slug: owner/name of the GitHub repository this checkout belongs to.
repo_slug() {
  gh repo view --json nameWithOwner -q .nameWithOwner
}

# sha_on_origin SHA: true when a branch on origin contains SHA (after the last fetch).
sha_on_origin() {
  [ -n "$(git branch -r --contains "$1" 2>/dev/null | grep -E '^ *origin/' || true)" ]
}

# tree_clean: no staged, unstaged or untracked (not ignored) changes.
tree_clean() {
  [ -z "$(git status --porcelain --untracked-files=normal)" ]
}

# post_status SHA STATE DESCRIPTION: set the local-ci commit status on SHA. GitHub caps the description at 140
# characters.
post_status() {
  local sha=$1 state=$2 desc=$3 slug
  have gh || die "--post needs gh: $(install_hint gh)"
  slug=$(repo_slug) || die "gh repo view failed: is gh signed in (gh auth status)?"
  desc=${desc:0:140}
  gh api --silent "repos/$slug/statuses/$sha" -f state="$state" -f context="$K3CI_CONTEXT" -f description="$desc"
  note "posted $K3CI_CONTEXT=$state on ${sha:0:12} ($slug): $desc"
}

# strip_git_env: drop every GIT_* variable. A git hook exports GIT_DIR and friends; a test suite that runs git in a
# temporary repository would otherwise write into this one.
strip_git_env() {
  local v
  for v in $(compgen -e); do
    case $v in GIT_*) unset "$v" ;; esac
  done
}

# fmt_secs N: 75 -> 1m15s
fmt_secs() {
  if [ "$1" -ge 60 ]; then printf '%dm%02ds' $(($1 / 60)) $(($1 % 60)); else printf '%ds' "$1"; fi
}

# areas_for_files: read paths relative to the repository root on stdin, one per line; print the check.sh areas they
# touch, space separated, in K3CI_AREAS order. secrets always runs.
areas_for_files() {
  local f a areas=" secrets " out=""
  while IFS= read -r f; do
    case $f in
      core/* | install/*) areas="$areas core " ;;
      scripts/ci/* | .githooks/*) areas="$areas core shell " ;;
      tui/*) areas="$areas tui vendor " ;;
      panes/*) areas="$areas panes vendor " ;;
      VENDOR.toml | NOTICE | LICENSES/* | scripts/vendor_check.py) areas="$areas vendor " ;;
    esac
    case $f in
      scripts/*.py) areas="$areas core " ;;
      *.sh | install/*) areas="$areas shell " ;;
    esac
  done
  for a in $K3CI_AREAS; do
    case $areas in *" $a "*) out="$out $a" ;; esac
  done
  echo "${out# }"
}

# merge_scope FORCE TITLE LABELS LINES MERGES: how much of check.sh a pull request needs. stdin: the files it changes,
# one per line. FORCE is 1 for --full, LABELS comma separated, LINES the added plus deleted lines, MERGES the merge
# commits it has over Main. Prints one tab-separated line: full or scoped, the areas (comma separated), the reason.
merge_scope() {
  local force=$1 title=$2 labels=$3 lines=$4 merges=$5 f n=0 hit="" files="" why=""
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    n=$((n + 1))
    files="$files$f"$'\n'
    [ -n "$hit" ] && continue
    case $f in
      VENDOR.toml | install/* | scripts/ci/*) hit=$f ;;
      uv.lock | */uv.lock | package-lock.json | */package-lock.json | go.mod | */go.mod | go.sum | */go.sum) hit=$f ;;
    esac
  done
  [ "$force" = 1 ] && why="$why; --full"
  case ",$labels," in *,full-ci,*) why="$why; label full-ci" ;; esac
  case $(printf '%s' "$title" | tr '[:upper:]' '[:lower:]') in
    feat* | *feature*) why="$why; the title names a feature" ;;
  esac
  [ "$n" -gt "$K3CI_FULL_FILES" ] && why="$why; $n files (over $K3CI_FULL_FILES)"
  [ "$lines" -gt "$K3CI_FULL_LINES" ] && why="$why; $lines changed lines (over $K3CI_FULL_LINES)"
  [ "$merges" -gt 1 ] && why="$why; $merges merge commits over Main (an integration branch)"
  [ -n "$hit" ] && why="$why; touches $hit"
  if [ -n "$why" ]; then
    printf 'full\t%s\t%s\n' "$(echo "$K3CI_AREAS" | tr ' ' ',')" "${why#; }"
  else
    printf 'scoped\t%s\t%s\n' "$(printf '%s' "$files" | areas_for_files | tr ' ' ',')" \
      "$n files, $lines changed lines, nothing that needs the full check"
  fi
}
