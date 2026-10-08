#!/bin/sh
# k3code installer. POSIX sh, no root, safe to re-run.
#
#   curl -fsSL https://raw.githubusercontent.com/K3NOXOFFICIAL/k3code/Main/install/install.sh | sh
#   sh install/install.sh --from-source                 # the checkout this script belongs to
#   sh install/install.sh --from-git URL --ref REF      # a clone (default: the latest v* tag, else Main)
#
# Each run builds the requested version next to the existing ones, switches to it and keeps the version
# before it for rollback. Layout under PREFIX (default ~/.local):
#   PREFIX/bin/{k3code,k3}                                  links into share/k3code/current
#   PREFIX/share/k3code/versions/<ver>/{venv,tui,bin}
#   PREFIX/share/k3code/current -> versions/<ver>           previous: a file with the name of the version before
# The installer never runs onboarding; it ends by telling you to run `k3code onboard`.
# Env (mostly for tests): K3CODE_DATA, K3_BIN_DIR, K3_INSTALL_LOG, K3_NO_DOWNLOAD (= --no-install-deps),
#   K3_SKIP_PIP, K3_SKIP_TUI, K3_SKIP_GO, K3_STUB_VENV (fake core, no uv), K3_EDITABLE (--from-source only).
set -eu

DEFAULT_URL=https://github.com/K3NOXOFFICIAL/k3code.git
DATA=""
BIN=""
INSTALL_LOG=""
TMP=""
BUILDING=""

usage() {
  cat <<EOF
usage: install.sh [--from-git [URL] | --from-source] [options]

Source (default: --from-git $DEFAULT_URL):
  --from-git [URL]      clone URL and install that
  --from-source         install the checkout this script belongs to
  --ref REF             with --from-git: a tag, a branch or a full commit SHA (default: latest v* tag, else Main)
  --channel stable|dev  stable = latest tag (default); dev = Main
  --version X           same as --ref vX

Options:
  --prefix DIR          install into DIR/bin and DIR/share/k3code (default: ~/.local)
  --from-bundle FILE    after installing, import a k3code export (settings and sessions)
  --yes, -y             do not ask (installs uv without asking)
  --no-install-deps     install nothing (no uv, no Python); fail with the hints instead
  --check               print the platform and dependency report, change nothing
  --no-activate         build the version without switching to it (used by k3code update)
  --print-version       print the version name on stdout (used by k3code update)
  -h, --help

Optional and never installed by this script: node 18+ with npm (builds the TUI), go (builds the
k3 pane binary), bubblewrap (sandbox). k3code runs without them.
EOF
}

# ---- output ----------------------------------------------------------------
say() { # say TEXT: stderr and the install log, no prefix (hints stay copy-pasteable)
  printf '%s\n' "$*" >&2
  if [ -n "$INSTALL_LOG" ]; then printf '%s\n' "$*" >>"$INSTALL_LOG" 2>/dev/null || true; fi
}
log() { say "k3code-install: $*"; }
die() {
  log "ERROR: $*"
  exit 1
}

cleanup() {
  rc=$?
  if [ -n "$BUILDING" ]; then rm -rf "$BUILDING"; fi
  if [ -n "$TMP" ]; then rm -rf "$TMP"; fi
  if [ "$rc" -ne 0 ]; then say "k3code-install: FAILED (exit $rc)${INSTALL_LOG:+. Log: $INSTALL_LOG}"; fi
  exit "$rc"
}

# ---- helpers ---------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

ask() { # ask QUESTION: --yes answers yes; otherwise ask on the terminal
  if [ "$YES" = 1 ]; then return 0; fi
  if (: </dev/tty) 2>/dev/null; then
    printf '%s [y/N] ' "$1" >/dev/tty
    read -r ans </dev/tty || ans=""
    case "$ans" in y | Y | yes | YES) return 0 ;; esac
    return 1
  fi
  die "$1 (no terminal to ask on: re-run with --yes, or --no-install-deps)"
}

fetch() { # fetch URL FILE
  if have curl; then curl -fsSL "$1" -o "$2"; else wget -qO "$2" "$1"; fi
}

node_major() { "$1" --version 2>/dev/null | sed 's/^v//; s/\..*//'; }

# ---- platform and package manager ------------------------------------------
detect_platform() {
  OS_NAME=$(uname -s)
  case "$OS_NAME" in
    Linux)
      PLATFORM=Linux
      # shellcheck source=/dev/null
      DISTRO=$(. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-Linux}") || DISTRO=Linux
      ;;
    Darwin)
      PLATFORM=macOS
      DISTRO="macOS $(sw_vers -productVersion 2>/dev/null || true)"
      ;;
    *) die "unsupported OS: $OS_NAME (k3code runs on Linux and macOS; on Windows use WSL)" ;;
  esac
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64 | amd64 | aarch64 | arm64) ;;
    *) die "unsupported architecture: $ARCH (x86_64 and arm64/aarch64 are supported)" ;;
  esac
}

detect_pm() {
  PM=""
  for p in dnf apt-get pacman zypper apk brew; do
    if have "$p"; then
      PM=$p
      return 0
    fi
  done
  return 0
}

hint_cmd() { # hint_cmd NAME: the command that installs NAME here (run it yourself; no root is used)
  case "$1:$PM" in
    uv:brew) echo "brew install uv" ;;
    uv:*) echo "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
    python:*) echo "uv python install 3.12" ;;
    curl:dnf) echo "sudo dnf install -y curl ca-certificates" ;;
    curl:apt-get) echo "sudo apt-get install -y curl ca-certificates" ;;
    curl:pacman) echo "sudo pacman -S --needed curl ca-certificates" ;;
    curl:zypper) echo "sudo zypper install -y curl ca-certificates" ;;
    curl:apk) echo "sudo apk add curl ca-certificates" ;;
    curl:*) echo "install curl (or wget) with your system package manager" ;;
    git:brew) echo "xcode-select --install" ;;
    git:dnf) echo "sudo dnf install -y git" ;;
    git:apt-get) echo "sudo apt-get install -y git" ;;
    git:pacman) echo "sudo pacman -S --needed git" ;;
    git:zypper) echo "sudo zypper install -y git" ;;
    git:apk) echo "sudo apk add git" ;;
    git:*) echo "install git with your system package manager" ;;
    node:brew) echo "brew install node" ;;
    node:dnf) echo "sudo dnf install -y nodejs npm" ;;
    node:apt-get) echo "sudo apt-get install -y nodejs npm" ;;
    node:pacman) echo "sudo pacman -S --needed nodejs npm" ;;
    node:zypper) echo "sudo zypper install -y nodejs npm" ;;
    node:apk) echo "sudo apk add nodejs npm" ;;
    node:*) echo "download Node 22 LTS from https://nodejs.org/" ;;
    go:brew) echo "brew install go" ;;
    go:dnf) echo "sudo dnf install -y golang" ;;
    go:apt-get) echo "sudo apt-get install -y golang-go" ;;
    go:pacman) echo "sudo pacman -S --needed go" ;;
    go:zypper) echo "sudo zypper install -y go" ;;
    go:apk) echo "sudo apk add go" ;;
    go:*) echo "download Go from https://go.dev/dl/ (go build fetches its own toolchain if yours is old)" ;;
    bwrap:dnf) echo "sudo dnf install -y bubblewrap" ;;
    bwrap:apt-get) echo "sudo apt-get install -y bubblewrap" ;;
    bwrap:pacman) echo "sudo pacman -S --needed bubblewrap" ;;
    bwrap:zypper) echo "sudo zypper install -y bubblewrap" ;;
    bwrap:apk) echo "sudo apk add bubblewrap" ;;
    bwrap:*) echo "install bubblewrap with your system package manager" ;;
    *) echo "see the k3code README" ;;
  esac
}

# ---- dependency lookup -----------------------------------------------------
find_uv() { # sets UV; succeeds when uv is on PATH or in BIN
  UV=""
  if have uv; then
    UV=$(command -v uv)
  elif [ -x "$BIN/uv" ]; then
    UV=$BIN/uv
  fi
  [ -n "$UV" ]
}

find_python() { # sets PY_FOUND to the first python3 that is 3.12 or newer
  PY_FOUND=""
  for p in python3.14 python3.13 python3.12 python3; do
    c=$(command -v "$p" 2>/dev/null || true)
    [ -n "$c" ] || continue
    if "$c" -c 'import sys; sys.exit(sys.version_info[:2] < (3, 12))' 2>/dev/null; then
      PY_FOUND=$c
      return 0
    fi
  done
  return 0
}

find_node() { # sets NODE_OK=1 when node 18+ and npm work (also accepts a node from an older install)
  NODE_OK=0
  if have node && have npm && [ "$(node_major node)" -ge 18 ] 2>/dev/null; then
    NODE_OK=1
    return 0
  fi
  for n in "$DATA"/node/*/bin/node; do
    [ -x "$n" ] || continue
    if [ "$(node_major "$n")" -ge 18 ] 2>/dev/null && [ -x "$(dirname "$n")/npm" ]; then
      PATH="$(dirname "$n"):$PATH"
      export PATH
      NODE_OK=1
      return 0
    fi
  done
  return 0
}

item() { # item ok|missing NAME [NOTE]
  if [ "$1" = ok ]; then say "  [ok]      $2${3:+ ($3)}"; else say "  [missing] $2${3:+ -- $3}"; fi
}

report() {
  find_python
  find_node
  say "k3code-install: platform $PLATFORM $ARCH${DISTRO:+, $DISTRO}; package manager: ${PM:-none found}"
  say "dependencies:"
  if find_uv; then item ok "uv"; else
    item missing "uv" "required; installed from astral.sh after a notice (or by you, see below)"
    say "      $(hint_cmd uv)"
    if ! have curl && ! have wget; then
      item missing "curl or wget" "needed to install uv"
      say "      $(hint_cmd curl)"
    fi
  fi
  if [ -n "$PY_FOUND" ]; then item ok "python 3.12+" "$PY_FOUND"; else
    item missing "python 3.12+" "uv downloads a managed one when needed"
    say "      $(hint_cmd python)"
  fi
  if have git; then item ok "git"; else
    item missing "git" "needed for --from-git, the default"
    say "      $(hint_cmd git)"
  fi
  if [ "$NODE_OK" = 1 ]; then item ok "node 18+ with npm"; else
    item missing "node 18+ with npm" "optional: builds the TUI; without it k3code uses the line REPL"
    say "      $(hint_cmd node)"
  fi
  if have go; then item ok "go"; else
    item missing "go" "optional: builds the k3 pane binary"
    say "      $(hint_cmd go)"
  fi
  if [ "$PLATFORM" = Linux ]; then
    if have bwrap; then item ok "bubblewrap"; else
      item missing "bubblewrap" "optional: sandbox for unattended runs"
      say "      $(hint_cmd bwrap)"
    fi
  fi
  return 0
}

ensure_uv() {
  if find_uv; then return 0; fi
  if [ "$NO_DEPS" = 1 ]; then die "uv is missing and --no-install-deps is set; install it: $(hint_cmd uv)"; fi
  if ! have curl && ! have wget; then die "curl or wget is needed to install uv: $(hint_cmd curl)"; fi
  log "uv is missing. Installing it into $BIN with its official installer (https://astral.sh/uv/install.sh)."
  ask "Install uv now?" || die "uv is required: install it with '$(hint_cmd uv)' and re-run this installer"
  tmp=$(mktemp "${TMPDIR:-/tmp}/uv-install.XXXXXX")
  fetch https://astral.sh/uv/install.sh "$tmp"
  UV_NO_MODIFY_PATH=1 UV_INSTALL_DIR="$BIN" sh "$tmp" >&2 || die "the uv installer failed (output above); install uv yourself: $(hint_cmd uv)"
  rm -f "$tmp"
  find_uv || die "uv installation failed"
}

default_ref() { # latest v* tag on the remote, else Main
  if [ "$CHANNEL" = dev ]; then
    echo Main
    return 0
  fi
  t=$(git ls-remote --tags --refs --sort=-v:refname "$GIT_URL" 2>/dev/null |
    sed -n 's#.*refs/tags/\(v[0-9][^/]*\)$#\1#p' | head -n 1)
  echo "${t:-Main}"
}

# ---- source ----------------------------------------------------------------
acquire_source() {
  if [ "$FROM" = source ]; then
    d=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || d=""
    if [ -n "$d" ] && [ -f "$d/core/pyproject.toml" ] && [ -f "$d/VERSION" ]; then
      SRC_ROOT=$d
    elif [ -f ./core/pyproject.toml ] && [ -f ./VERSION ]; then
      SRC_ROOT=$(pwd)
    else
      die "--from-source must run from a k3code checkout (sh install/install.sh --from-source)"
    fi
    SOURCE_PATH=$SRC_ROOT
    SHA=$(git -C "$SRC_ROOT" rev-parse --short HEAD 2>/dev/null || true)
  else
    [ -n "$GIT_URL" ] || GIT_URL=$DEFAULT_URL
    [ -n "$REF" ] || REF=$(default_ref)
    TMP=$(mktemp -d "${TMPDIR:-/tmp}/k3code-src.XXXXXX")
    SRC_ROOT=$TMP/src
    mkdir "$SRC_ROOT"
    log "fetching $REF from $GIT_URL"
    git -c init.defaultBranch=main init -q "$SRC_ROOT"
    git -C "$SRC_ROOT" fetch -q --depth 1 "$GIT_URL" "$REF" ||
      die "could not fetch '$REF' from $GIT_URL. If the repo is private and git has no credentials: gh auth login && gh auth setup-git (or check --ref)"
    git -C "$SRC_ROOT" checkout -q FETCH_HEAD
    SHA=$(git -C "$SRC_ROOT" rev-parse --short HEAD)
  fi
  if [ ! -f "$SRC_ROOT/core/pyproject.toml" ] || [ ! -f "$SRC_ROOT/VERSION" ]; then
    die "not a k3code checkout (no core/pyproject.toml or VERSION): $SRC_ROOT"
  fi
  VER="$(tr -d '[:space:]' <"$SRC_ROOT/VERSION")-src${SHA:+.$SHA}"
  VERDIR="$DATA/versions/$VER"
  # The TUI build writes node_modules into its tree. A read-only checkout is built from a copy.
  if [ "$FROM" = source ] && [ ! -f "$VERDIR/.complete" ] && [ "${K3_EDITABLE:-0}" != 1 ] &&
    [ "${K3_SKIP_TUI:-0}" != 1 ] && [ ! -w "$SRC_ROOT/tui" ]; then
    log "the checkout is read-only: building from a temporary copy"
    TMP=$(mktemp -d "${TMPDIR:-/tmp}/k3code-src.XXXXXX")
    cp -R "$SRC_ROOT" "$TMP/src"
    SRC_ROOT=$TMP/src
  fi
  return 0
}

# ---- build -----------------------------------------------------------------
build_tui() {
  if [ "${K3_SKIP_TUI:-0}" = 1 ]; then return 0; fi
  if [ "$NODE_OK" != 1 ]; then
    log "TUI skipped: node 18+ with npm not found (k3code falls back to the line REPL)"
    return 0
  fi
  log "building the TUI (npm ci; this takes a minute)"
  if (cd "$SRC_ROOT/tui" && npm ci --no-audit --no-fund >&2 && npm run build:ink >&2 && npm run build >&2) &&
    [ -d "$SRC_ROOT/tui/dist" ]; then
    cp -R "$SRC_ROOT/tui/dist" "$VERDIR/tui/dist"
  else
    log "WARNING: the TUI did not build (output above); k3code will use the line REPL"
  fi
  return 0
}

build_panes() {
  if [ "${K3_SKIP_GO:-0}" = 1 ]; then return 0; fi
  if ! have go; then
    log "k3 pane binary skipped: go not found (optional)"
    return 0
  fi
  log "building the k3 pane binary"
  if ! (cd "$SRC_ROOT/panes" && CGO_ENABLED=0 go build -o "$VERDIR/bin/k3" ./cmd/k3 >&2); then
    log "WARNING: the k3 pane binary did not build (output above); the multi-window binary is missing"
  fi
  return 0
}

install_version() {
  if [ -f "$VERDIR/.complete" ]; then
    log "version $VER already installed"
    return 0
  fi
  log "installing version $VER into $VERDIR"
  rm -rf "$VERDIR"
  mkdir -p "$VERDIR"
  BUILDING=$VERDIR
  if [ "${K3_STUB_VENV:-0}" = 1 ]; then # tests: a fake core command, no uv, no network
    mkdir -p "$VERDIR/venv/bin"
    printf '#!/bin/sh\necho "k3code %s"\n' "$VER" >"$VERDIR/venv/bin/k3code"
    chmod +x "$VERDIR/venv/bin/k3code"
  else
    "$UV" venv --quiet --python '>=3.12' "$VERDIR/venv" >&2 ||
      die "could not create a Python 3.12+ environment; try: $(hint_cmd python)"
    if [ "${K3_SKIP_PIP:-0}" != 1 ]; then
      log "installing the k3code core"
      if [ "${K3_EDITABLE:-0}" = 1 ]; then
        "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" -e "$SRC_ROOT/core" >&2
      else
        # A regular copy: the installed k3code must not depend on a checkout that can be switched or deleted.
        "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" "$SRC_ROOT/core" >&2
      fi
    fi
  fi
  mkdir -p "$VERDIR/tui" "$VERDIR/bin"
  build_tui
  build_panes
  if [ "${K3_SKIP_PIP:-0}" != 1 ]; then
    "$VERDIR/venv/bin/k3code" --version >&2 || die "the new version does not start (k3code --version failed); nothing was activated"
  fi
  if [ "$FROM" = source ]; then printf '%s\n' "$SOURCE_PATH" >"$DATA/source_path"; fi
  printf '%s\n' "$VER" >"$VERDIR/.complete"
  BUILDING=""
  return 0
}

# ---- activation ------------------------------------------------------------
link_bin() { # link_bin NAME TARGET: BIN/NAME -> TARGET; drops a stale link of ours
  if [ -e "$2" ]; then
    if [ "$(readlink "$BIN/$1" 2>/dev/null || true)" != "$2" ]; then ln -sfn "$2" "$BIN/$1"; fi
  else
    case "$(readlink "$BIN/$1" 2>/dev/null || true)" in
      "$DATA"/*) rm -f "$BIN/$1" ;;
    esac
  fi
  return 0
}

prune_versions() { # keep the current and the previous version only
  prev=$(cat "$DATA/previous" 2>/dev/null || true)
  for d in "$DATA"/versions/*; do
    [ -d "$d" ] || continue
    n=$(basename "$d")
    case "$n" in "$VER" | "$prev") continue ;; esac
    rm -rf "$d"
    log "removed old version $n"
  done
  return 0
}

activate() {
  cur=""
  if [ -L "$DATA/current" ]; then cur=$(basename "$(readlink "$DATA/current")"); fi
  if [ "$cur" != "$VER" ]; then
    if [ -n "$cur" ]; then printf '%s\n' "$cur" >"$DATA/previous"; fi
    ln -sfn "$VERDIR" "$DATA/current"
    log "current -> $VER${cur:+ (previous: $cur, kept for rollback)}"
  fi
  if [ "$FROM" = git ] && [ -e "$DATA/source_path" ]; then rm -f "$DATA/source_path"; fi
  link_bin k3code "$DATA/current/venv/bin/k3code"
  link_bin k3 "$DATA/current/bin/k3"
  prune_versions
}

import_bundle() {
  [ -f "$BUNDLE" ] || die "bundle not found: $BUNDLE"
  log "importing $BUNDLE (settings and sessions; secrets are not in bundles)"
  "$BIN/k3code" import "$BUNDLE" --yes >&2 || die "import of $BUNDLE failed"
}

# ---- main ------------------------------------------------------------------
main() {
  export GIT_TERMINAL_PROMPT=0
  FROM="" GIT_URL="" REF="" CHANNEL=stable WANT_VERSION="" PREFIX="$HOME/.local" BUNDLE=""
  YES=0 NO_DEPS=0 CHECK=0 ACTIVATE=1 PRINT_VERSION=0
  if [ "${K3_NO_DOWNLOAD:-0}" = 1 ]; then NO_DEPS=1; fi
  while [ $# -gt 0 ]; do
    case "$1" in
      --from-source) FROM=source ;;
      --from-git)
        FROM=git
        if [ $# -ge 2 ]; then
          case "$2" in
            -*) ;;
            *)
              GIT_URL=$2
              shift
              ;;
          esac
        fi
        ;;
      --ref)
        [ $# -ge 2 ] || die "--ref needs a value"
        REF=$2
        shift
        ;;
      --channel)
        [ $# -ge 2 ] || die "--channel needs a value"
        CHANNEL=$2
        shift
        ;;
      --version)
        [ $# -ge 2 ] || die "--version needs a value"
        WANT_VERSION=${2#v}
        shift
        ;;
      --prefix)
        [ $# -ge 2 ] || die "--prefix needs a directory"
        PREFIX=$2
        shift
        ;;
      --from-bundle)
        [ $# -ge 2 ] || die "--from-bundle needs a file"
        BUNDLE=$2
        shift
        ;;
      --yes | -y) YES=1 ;;
      --no-install-deps) NO_DEPS=1 ;;
      --check) CHECK=1 ;;
      --no-activate) ACTIVATE=0 ;;
      --print-version) PRINT_VERSION=1 ;;
      --no-setup | --headless) ;; # legacy no-ops: setup no longer runs here; old callers (k3code update) still pass it
      -h | --help)
        usage
        exit 0
        ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
  done
  case "$CHANNEL" in stable | dev) ;; *) die "--channel must be stable or dev" ;; esac
  if [ -n "$BUNDLE" ] && [ ! -f "$BUNDLE" ]; then die "bundle not found: $BUNDLE (checked before installing anything)"; fi
  if [ -n "$WANT_VERSION" ] && [ -z "$REF" ]; then REF="v$WANT_VERSION"; fi
  if [ -z "$FROM" ]; then FROM=git; fi
  if [ "${K3_EDITABLE:-0}" = 1 ] && [ "$FROM" = git ]; then
    die "K3_EDITABLE=1 needs --from-source (a --from-git build comes from a temporary clone)"
  fi
  DATA="${K3CODE_DATA:-$PREFIX/share/k3code}"
  BIN="${K3_BIN_DIR:-$PREFIX/bin}"
  trap cleanup EXIT
  trap 'exit 1' INT TERM
  detect_platform
  detect_pm
  if [ "$CHECK" = 1 ]; then
    report
    exit 0
  fi
  mkdir -p "$DATA" "$BIN"
  INSTALL_LOG="${K3_INSTALL_LOG:-$DATA/install.log}"
  printf '\n==== %s install start (args: %s) ====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$INSTALL_LOG" 2>/dev/null || true
  report
  if [ "$NO_DEPS" = 1 ]; then export UV_PYTHON_DOWNLOADS=never; fi
  if [ "$FROM" = git ] && ! have git; then die "git is needed for --from-git: $(hint_cmd git)"; fi
  ensure_uv
  if [ -z "$PY_FOUND" ] && [ "$NO_DEPS" != 1 ]; then
    log "no Python 3.12+ here: uv will download a managed one (kept in uv's own data directory)"
  fi
  acquire_source
  install_version
  if [ "$ACTIVATE" = 1 ]; then activate; fi
  if [ "$PRINT_VERSION" = 1 ]; then
    printf '%s\n' "$VER"
    exit 0
  fi
  if [ "$ACTIVATE" != 1 ]; then exit 0; fi
  if [ -n "$BUNDLE" ]; then import_bundle; fi
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "Add $BIN to your PATH:  export PATH=\"$BIN:\$PATH\"   (in ~/.bashrc or ~/.zshrc)" ;;
  esac
  if [ "$DATA" != "$HOME/.local/share/k3code" ]; then
    say "For a non-default prefix, 'k3code update' needs:  export K3CODE_DATA=\"$DATA\""
  fi
  # shellcheck disable=SC2016 # the backticks are literal text for the user
  say "$(printf 'Installed k3code %s. Next: run `k3code onboard` to set up your provider (or `k3code` to start).' "$VER")"
}

main "$@" </dev/null
