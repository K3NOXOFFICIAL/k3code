#!/bin/sh
# k3code installer. POSIX sh, no root, safe to re-run.
#
#   curl -fsSL https://raw.githubusercontent.com/K3NOXOFFICIAL/k3code/Main/install/install.sh | sh
#   sh install/install.sh --from-source                 # the checkout this script belongs to
#   sh install/install.sh --from-git URL --ref REF      # a clone (default: the latest v* tag, else Main)
#
# Runs on Linux and macOS (x86_64, arm64). On Windows, run install\install.ps1 from PowerShell: it installs
# k3code into WSL with this script and adds k3code/k3 commands to Windows.
#
# Each run builds the requested version next to the existing ones, switches to it and keeps the version
# before it for rollback. Layout under PREFIX (default ~/.local):
#   PREFIX/bin/{k3code,k3}                                  links into share/k3code/current
#   PREFIX/share/k3code/versions/<ver>/{venv,tui,bin}
#   PREFIX/share/k3code/current -> versions/<ver>           previous: a file with the name of the version before
# Everything k3code needs is fetched into your home directory, without root: uv (and through it Python),
# a private Node runtime (PREFIX/share/k3code/node, for the TUI) and a Go toolchain (PREFIX/share/k3code/go,
# to build the k3 pane binary) when they are not on PATH. bubblewrap is installed through the system package
# manager only when that works without a password (root or passwordless sudo). --no-install-deps fetches
# nothing. The installer never runs onboarding; it ends by telling you to run `k3code onboard`.
# Presetup (after activation; --minimal skips it) checks the sandbox, installs Chromium for the browser tool and
# prints a health subset. It never fails the install.
# Env: K3CODE_SKIP_CHROMIUM=1 (presetup: no Chromium). Mostly for tests: K3CODE_DATA, K3_BIN_DIR, K3_INSTALL_LOG,
#   K3_NO_DOWNLOAD (= --no-install-deps), K3_SKIP_PIP, K3_SKIP_TUI, K3_SKIP_GO, K3_STUB_VENV (fake core, no uv,
#   no network), K3_EDITABLE (--from-source only), K3_BWRAP (the bubblewrap binary to probe).
set -eu

DEFAULT_URL=https://github.com/K3NOXOFFICIAL/k3code.git
DATA=""
BIN=""
INSTALL_LOG=""
TMP=""
BUILDING=""
REQS=""
UV_TMP=""
GIT_ERR=""
LOCK=""

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
  --yes, -y             accepted for older callers; with it, presetup never prompts for sudo
  --no-install-deps     install nothing (no uv, Python, Node, Go); fail or skip with the hints instead
  --minimal             skip presetup: no sandbox check, no Chromium, no doctor subset
  --check               print the platform and dependency report, change nothing
  --allow-root          install as root anyway (for example in a container); refused by default
  --force               replace a k3code or k3 in the bin directory that this installer did not create
  --no-activate         build the version without switching to it (used by k3code update)
  --print-version       print the version name on stdout (used by k3code update)
  -h, --help

Fetched when missing (into PREFIX/share/k3code, no root): uv, Python 3.12+, Node 22 (the TUI), Go (the k3
pane binary). bubblewrap (sandbox, Linux) is installed only when root or passwordless sudo is available.

Presetup (on by default, after the version is activated; never fails the install): checks the sandbox, installs
Chromium for the browser tool (about 115 MiB; K3CODE_SKIP_CHROMIUM=1 skips only that) and prints a health subset.
Without bubblewrap it prints the install command and runs nothing; it asks before a sudo run only on a terminal.
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
  if [ -n "$REQS" ]; then rm -f "$REQS"; fi
  if [ -n "$UV_TMP" ]; then rm -f "$UV_TMP"; fi
  if [ -n "$GIT_ERR" ]; then rm -f "$GIT_ERR"; fi
  if [ -n "$LOCK" ]; then rm -rf "$LOCK"; fi
  if [ "$rc" -ne 0 ]; then say "k3code-install: FAILED (exit $rc)${INSTALL_LOG:+. Log: $INSTALL_LOG}"; fi
  exit "$rc"
}

# ---- helpers ---------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

fetch() { # fetch URL FILE
  if have curl; then curl -fsSL --retry 3 "$1" -o "$2"; else wget -q --tries=3 -O "$2" "$1"; fi
}

ask_tty() { # ask_tty QUESTION: asks on the terminal only, never with --yes; no terminal means no
  if [ "$YES" = 1 ] || ! (: </dev/tty) 2>/dev/null; then return 1; fi
  printf '%s [y/N] ' "$1" >/dev/tty
  read -r ans </dev/tty || ans=""
  case "$ans" in y | Y | yes | YES) return 0 ;; esac
  return 1
}

sha256_of() { # sha256_of FILE
  if have sha256sum; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

can_fetch() { have curl || have wget; }

as_root() { # as_root CMD...: root directly, or through sudo only after a person said yes at a terminal
  if [ "$(id -u)" = 0 ]; then "$@"; elif [ "${ROOT_APPROVED:-0}" = 1 ]; then sudo "$@"; else return 1; fi
}

node_major() { "$1" --version 2>/dev/null | sed 's/^v//; s/\..*//'; }

home_of() { # home_of USER: that user's home directory, empty when unknown
  case "$1" in '' | *[!A-Za-z0-9._-]*) return 0 ;; esac
  h=$(eval "printf '%s' ~$1")
  case "$h" in /*) printf '%s' "$h" ;; esac
}

# The install belongs to the user whose home it is in. Root (sudo included) would leave root-owned files there, or
# install into root's home; --allow-root is for a deliberate root install such as a container.
check_user() {
  if [ "$ALLOW_ROOT" = 1 ]; then return 0; fi
  if [ "$(id -u)" = 0 ]; then
    die "refusing to run as root${SUDO_USER:+ (through sudo)}: run the installer as the user who will use k3code, without sudo (--allow-root installs for root, for example in a container)"
  fi
  if [ -n "${SUDO_USER:-}" ]; then # sudo -u: HOME must be the home of the user this runs as
    h=$(home_of "$(id -un)")
    if [ -n "$h" ] && [ "$h" != "$HOME" ]; then
      die "HOME is $HOME but this runs as $(id -un) (home $h) through sudo: run it as that user with their HOME (sudo -H), or as yourself without sudo"
    fi
  fi
  return 0
}

take_lock() { # one installer at a time per install root; the lock goes when this run exits
  if mkdir "$DATA/.install.lock" 2>/dev/null; then
    LOCK=$DATA/.install.lock
    printf '%s\n' "$$" >"$LOCK/pid"
    return 0
  fi
  pid=$(cat "$DATA/.install.lock/pid" 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    die "another install into $DATA is running (pid $pid); wait for it to finish"
  fi
  die "an install lock from pid ${pid:-unknown} is left in $DATA/.install.lock, and that process is not running: if no other install runs, remove it (rm -r '$DATA/.install.lock') and re-run"
}

# ---- platform and package manager ------------------------------------------
detect_platform() {
  OS_NAME=$(uname -s)
  case "$OS_NAME" in
    Linux)
      PLATFORM=Linux
      # shellcheck source=/dev/null
      DISTRO=$(. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-Linux}") || DISTRO=Linux
      if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then DISTRO="$DISTRO, WSL"; fi
      ;;
    Darwin)
      PLATFORM=macOS
      DISTRO="macOS $(sw_vers -productVersion 2>/dev/null || true)"
      ;;
    MINGW* | MSYS* | CYGWIN* | Windows_NT)
      die "this is a Windows shell ($OS_NAME). k3code runs in WSL on Windows: in PowerShell run install\\install.ps1"
      ;;
    *) die "unsupported OS: $OS_NAME (k3code runs on Linux and macOS; on Windows use install\\install.ps1)" ;;
  esac
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64 | amd64 | aarch64 | arm64) ;;
    *) die "unsupported architecture: $ARCH (x86_64 and arm64/aarch64 are supported)" ;;
  esac
  case "$ARCH" in x86_64 | amd64) NODE_ARCH=x64 GO_ARCH=amd64 ;; *) NODE_ARCH=arm64 GO_ARCH=arm64 ;; esac
  case "$PLATFORM" in Linux) NODE_OS=linux GO_OS=linux ;; *) NODE_OS=darwin GO_OS=darwin ;; esac
  MUSL=0
  if [ "$PLATFORM" = Linux ] && ls /lib/ld-musl-* >/dev/null 2>&1; then MUSL=1; fi # Alpine: no official Node build
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
  if [ "$PLATFORM" = macOS ]; then
    case "$1" in
      git) echo "xcode-select --install" && return 0 ;;
      curl) echo "curl ships with macOS; check your PATH" && return 0 ;;
    esac
  fi
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

find_node() { # sets NODE_OK=1 when node 20+ and npm work (also accepts a node from an older install)
  NODE_OK=0
  if have node && have npm && [ "$(node_major node)" -ge 20 ] 2>/dev/null; then
    NODE_OK=1
    return 0
  fi
  for n in "$DATA"/node/*/bin/node; do
    [ -x "$n" ] || continue
    if [ "$(node_major "$n")" -ge 20 ] 2>/dev/null && [ -x "$(dirname "$n")/npm" ]; then
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
  if [ "$NODE_OK" = 1 ]; then item ok "node 20+ with npm"; else
    if [ "$NO_DEPS" != 1 ] && [ "$MUSL" != 1 ] && can_fetch; then
      item missing "node 20+ with npm" "a private Node 22 is fetched into $DATA/node for the TUI"
    else
      item missing "node 20+ with npm" "optional: builds the TUI; without it k3code uses the line REPL"
      say "      $(hint_cmd node)"
    fi
  fi
  if find_go; then item ok "go" "$GO"; else
    if [ "$NO_DEPS" != 1 ] && can_fetch; then
      item missing "go" "a Go toolchain is fetched into $DATA/go to build the k3 pane binary"
    else
      item missing "go" "optional: builds the k3 pane binary"
      say "      $(hint_cmd go)"
    fi
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
  log "uv is missing: installing it into $BIN with its official installer (https://astral.sh/uv/install.sh)"
  UV_TMP=$(mktemp "${TMPDIR:-/tmp}/uv-install.XXXXXX")
  fetch https://astral.sh/uv/install.sh "$UV_TMP" ||
    die "could not download the uv installer (no network?); install uv yourself: $(hint_cmd uv)"
  UV_NO_MODIFY_PATH=1 UV_INSTALL_DIR="$BIN" sh "$UV_TMP" >&2 || die "the uv installer failed (output above); install uv yourself: $(hint_cmd uv)"
  rm -f "$UV_TMP"
  UV_TMP=""
  find_uv || die "uv installation failed"
}

find_go() { # sets GO to a go on PATH, else the newest one fetched into DATA/go
  GO=""
  if have go; then
    GO=$(command -v go)
    return 0
  fi
  for g in "$DATA"/go/go*/bin/go; do
    if [ -x "$g" ]; then GO=$g; fi
  done
  [ -n "$GO" ]
}

ensure_node() { # a private Node 22 LTS in DATA/node/<ver>, checked against nodejs.org's SHASUMS256.txt
  find_node
  if [ "$NODE_OK" = 1 ] || [ "$NO_DEPS" = 1 ] || [ "${K3_SKIP_TUI:-0}" = 1 ]; then return 0; fi
  if [ "$MUSL" = 1 ] || ! can_fetch; then return 0; fi
  base=https://nodejs.org/dist/latest-v22.x
  t=$(mktemp -d "${TMPDIR:-/tmp}/k3code-node.XXXXXX")
  if ! fetch "$base/SHASUMS256.txt" "$t/SHASUMS256.txt"; then
    log "WARNING: could not reach nodejs.org; the TUI is skipped (k3code uses the line REPL)"
    rm -rf "$t"
    return 0
  fi
  line=$(grep " node-v[0-9.]*-$NODE_OS-$NODE_ARCH\.tar\.gz\$" "$t/SHASUMS256.txt" | head -n 1)
  name=${line##* }
  want=${line%% *}
  nver=${name%-"$NODE_OS"-*}
  log "fetching $nver ($NODE_OS-$NODE_ARCH) into $DATA/node for the TUI"
  if fetch "$base/$name" "$t/$name" && [ "$(sha256_of "$t/$name")" = "$want" ] &&
    mkdir -p "$DATA/node" && tar -xzf "$t/$name" -C "$t" && rm -rf "${DATA:?}/node/$nver" &&
    mv "$t/${name%.tar.gz}" "$DATA/node/$nver"; then
    for d in "$DATA"/node/*; do # keep only this Node
      if [ "$d" != "$DATA/node/$nver" ]; then rm -rf "$d"; fi
    done
  else
    log "WARNING: the Node download failed or did not match its checksum; the TUI is skipped"
  fi
  rm -rf "$t"
  find_node
}

ensure_go() { # the Go toolchain panes/go.mod asks for, from the Go module proxy, in DATA/go/go<ver>
  if [ "${K3_SKIP_GO:-0}" = 1 ] || find_go || [ "$NO_DEPS" = 1 ] || ! can_fetch; then return 0; fi
  gv=$(sed -n 's/^go \([0-9][0-9.]*\)$/\1/p' "$SRC_ROOT/panes/go.mod" | head -n 1)
  [ -n "$gv" ] || return 0
  name="v0.0.1-go$gv.$GO_OS-$GO_ARCH"
  t=$(mktemp -d "${TMPDIR:-/tmp}/k3code-go.XXXXXX")
  log "fetching Go $gv ($GO_OS-$GO_ARCH) into $DATA/go to build the k3 pane binary"
  if fetch "https://proxy.golang.org/golang.org/toolchain/@v/$name.zip" "$t/go.zip" && unpack_zip "$t/go.zip" "$t"; then
    mkdir -p "$DATA/go"
    rm -rf "$DATA/go/go$gv"
    mv "$t/golang.org/toolchain@$name" "$DATA/go/go$gv"
    for d in "$DATA"/go/go*; do # keep only this toolchain
      if [ "$d" != "$DATA/go/go$gv" ]; then rm -rf "$d"; fi
    done
  else
    log "WARNING: the Go download failed; the k3 pane binary is skipped"
  fi
  rm -rf "$t"
  find_go || true
}

unpack_zip() { # unpack_zip ZIP DIR: unzip, else Python's zipfile (which drops the exec bits: restore them)
  if have unzip; then
    unzip -q "$1" -d "$2"
    return $?
  fi
  py=$PY_FOUND
  if [ -z "$py" ] && [ -x "$VERDIR/venv/bin/python" ]; then py=$VERDIR/venv/bin/python; fi
  [ -n "$py" ] || return 1
  "$py" -m zipfile -e "$1" "$2" || return 1
  for x in "$2"/golang.org/toolchain@*/bin "$2"/golang.org/toolchain@*/pkg/tool; do
    if [ -d "$x" ]; then chmod -R u+x "$x"; fi
  done
}

ensure_bwrap() { # Linux sandbox: through the package manager, as root or after a yes at a terminal
  if [ "$PLATFORM" != Linux ] || have bwrap || [ "$NO_DEPS" = 1 ] || [ -z "$PM" ] || [ "$PM" = brew ]; then return 0; fi
  # Never sudo unattended: a passwordless sudo is not consent. Root installs directly; anyone else is asked at a
  # terminal (never with --yes, never without a tty), and sudo then asks for the password itself.
  if [ "$(id -u)" != 0 ] && [ "${ROOT_APPROVED:-0}" != 1 ] && [ "$YES" != 1 ] && [ -t 0 ] && [ -t 1 ] && have sudo; then
    printf 'Install bubblewrap now with sudo (it asks for your password)? [y/N] ' >&2
    read -r ans || ans=
    case "$ans" in y | Y | yes | YES) ROOT_APPROVED=1 ;; esac
  fi
  if [ "$(id -u)" != 0 ] && [ "${ROOT_APPROVED:-0}" != 1 ]; then
    log "bubblewrap (the sandbox for unattended runs) needs root to install: $(hint_cmd bwrap)"
    return 0
  fi
  log "installing bubblewrap with $PM (the sandbox for unattended runs)"
  case "$PM" in
    dnf) as_root dnf install -y bubblewrap >&2 ;;
    apt-get) # a fresh image has no package index yet
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y bubblewrap >&2 ||
        { as_root apt-get update -q >&2 && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y bubblewrap >&2; }
      ;;
    pacman) as_root pacman -S --needed --noconfirm bubblewrap >&2 ;;
    zypper) as_root zypper --non-interactive install bubblewrap >&2 ;;
    apk) as_root apk add bubblewrap >&2 ;;
  esac || log "WARNING: bubblewrap did not install; k3code runs unattended commands without the sandbox"
  return 0
}

default_ref() { # latest v* tag on the remote, else Main; fails (prints nothing) when the remote cannot be reached
  if [ "$CHANNEL" = dev ]; then
    echo Main
    return 0
  fi
  out=$(git ls-remote --tags --refs --sort=-v:refname "$GIT_URL" 2>"$GIT_ERR") || return 1
  t=$(printf '%s\n' "$out" | sed -n 's#.*refs/tags/\(v[0-9][^/]*\)$#\1#p' | head -n 1)
  echo "${t:-Main}"
}

# Network failures are told apart from private-repo failures: the git error text decides.
git_error_reason() { # prints private, offline or other, from the last git error in GIT_ERR
  if grep -qiE 'terminal prompts disabled|could not read username|authentication failed|repository not found|returned error: (401|403)' "$GIT_ERR" 2>/dev/null; then
    echo private
  elif grep -qiE 'could not resolve host|temporary failure in name resolution|failed to connect|network is unreachable|connection (timed out|refused|reset)|operation timed out' "$GIT_ERR" 2>/dev/null; then
    echo offline
  else
    echo other
  fi
}

git_error_hint() { # git_error_hint REASON: what to tell the user after "could not fetch"
  case "$1" in
    offline) echo "no network: check the connection and re-run" ;;
    private) echo "the repo is private or needs credentials: gh auth login && gh auth setup-git (or check --ref)" ;;
    *) echo "git said: $(tail -n 1 "$GIT_ERR" 2>/dev/null)" ;;
  esac
}

# The installed version built from REF (a complete one; the active version wins), empty when there is none.
recorded_version() {
  if [ -f "$DATA/current/.complete" ] && [ "$(cat "$DATA/current/.ref" 2>/dev/null)" = "$1" ]; then
    basename "$(readlink "$DATA/current")"
    return 0
  fi
  found=""
  for d in "$DATA"/versions/*; do
    [ -f "$d/.complete" ] && [ "$(cat "$d/.ref" 2>/dev/null)" = "$1" ] && found=$(basename "$d")
  done
  printf '%s' "$found"
}

installed_ref() { # the ref the active version was built from, if it was recorded
  if [ -f "$DATA/current/.ref" ]; then cat "$DATA/current/.ref"; fi
  return 0
}

checkout_root() { # the k3code checkout this script sits in, if any
  d=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || return 1
  [ -f "$d/core/pyproject.toml" ] && [ -f "$d/VERSION" ] && printf '%s' "$d"
}

# Sets REF: the latest tag on the remote, else Main; offline, the ref of the active version.
# Returns 3 when the remote is unreachable, nothing is installed yet and this script runs from a checkout of the
# default repository: the caller then installs that checkout (for example a Windows clone made with Windows git,
# whose credentials the WSL side does not have).
pick_default_ref() {
  if REF=$(default_ref); then return 0; fi
  REF=$(installed_ref)
  if [ -z "$REF" ]; then
    if [ "$GIT_URL" = "$DEFAULT_URL" ] && checkout_root >/dev/null; then
      log "could not reach $GIT_URL ($(git_error_hint "$(git_error_reason)")): installing this checkout instead (--from-source)"
      return 3
    fi
    die "could not reach $GIT_URL to find the latest version: $(git_error_hint "$(git_error_reason)"). Pass --ref to choose one"
  fi
  log "could not reach $GIT_URL for the latest version: keeping the installed $REF"
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
    # Uncommitted edits get their own version (a checksum of the changes), so they are not hidden by the
    # build of the clean HEAD.
    if [ -n "$SHA" ] && [ -n "$(git -C "$SRC_ROOT" status --porcelain --untracked-files=normal 2>/dev/null)" ]; then
      dirty=$( (git -C "$SRC_ROOT" diff HEAD && git -C "$SRC_ROOT" ls-files --others --exclude-standard |
        while IFS= read -r f; do cat "$SRC_ROOT/$f"; done) 2>/dev/null | cksum | cut -d' ' -f1)
      SHA="$SHA.dirty$dirty"
    fi
  else
    [ -n "$GIT_URL" ] || GIT_URL=$DEFAULT_URL
    GIT_ERR=$(mktemp "${TMPDIR:-/tmp}/k3code-giterr.XXXXXX")
    if [ -z "$REF" ]; then
      if ! pick_default_ref; then
        FROM=source
        acquire_source
        return 0
      fi
    fi
    # A tag never moves, so a complete install of it needs no network at all (this works offline).
    case "$REF" in
      v[0-9]*)
        VER=$(recorded_version "$REF")
        if [ -n "$VER" ]; then
          VERDIR=$DATA/versions/$VER
          SRC_ROOT=""
          return 0
        fi
        ;;
    esac
    TMP=$(mktemp -d "${TMPDIR:-/tmp}/k3code-src.XXXXXX")
    SRC_ROOT=$TMP/src
    mkdir "$SRC_ROOT"
    log "fetching $REF from $GIT_URL"
    git -c init.defaultBranch=main init -q "$SRC_ROOT"
    if ! git -C "$SRC_ROOT" fetch -q --depth 1 "$GIT_URL" "$REF" 2>"$GIT_ERR"; then
      msg="could not fetch '$REF' from $GIT_URL: $(git_error_hint "$(git_error_reason)")"
      # A branch moves, so it is always fetched first; the installed build of it is the fallback when that fails.
      VER=$(recorded_version "$REF")
      if [ -z "$VER" ]; then die "$msg"; fi
      log "$msg. Using the installed version $VER"
      VERDIR=$DATA/versions/$VER
      SRC_ROOT=""
      return 0
    fi
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
    log "TUI skipped: node 20+ with npm not found (k3code falls back to the line REPL)"
    return 0
  fi
  log "building the TUI (npm ci; this takes a minute)"
  if (cd "$SRC_ROOT/tui" && npm ci --no-audit --no-fund >&2 && npm run build:ink >&2 && npm run build >&2) &&
    [ -d "$SRC_ROOT/tui/dist" ]; then
    cp -R "$SRC_ROOT/tui/dist" "$VERDIR/tui/dist"
    # Build receipts belong to the build tooling; nothing at runtime reads them.
    rm -f "$VERDIR/tui/dist/hermes-build.json" "$VERDIR/tui/dist/.k3code-product"
  else
    log "WARNING: the TUI did not build (output above); k3code will use the line REPL"
  fi
  return 0
}

build_panes() {
  if [ "${K3_SKIP_GO:-0}" = 1 ]; then return 0; fi
  if ! find_go; then
    log "k3 pane binary skipped: go not found (optional)"
    return 0
  fi
  log "building the k3 pane binary"
  case "$GO" in
    "$DATA"/*) set -- env GOPATH="$DATA/go/path" GOFLAGS=-modcacherw GOTOOLCHAIN=local ;; # keep ~/go untouched
    *) set -- env ;;
  esac
  if ! (cd "$SRC_ROOT/panes" && "$@" CGO_ENABLED=0 "$GO" build -trimpath -ldflags "-s -w" -o "$VERDIR/bin/k3" ./cmd/k3 >&2); then
    log "WARNING: the k3 pane binary did not build (output above); the multi-window binary is missing"
  fi
  if [ -d "$DATA/go/path" ]; then rm -rf "$DATA/go/path"; fi # the module cache is only needed during the build
  return 0
}

# The installed core is a regular copy (it must not depend on a checkout that can be switched or deleted).
# Its runtime dependencies are the locked set from core/uv.lock without the dev group, so the install is the
# set CI tested. --locked fails on a stale lock (a missing dependency) instead of installing the stale set.
# A checkout without a lock file (an older tag) resolves the dependencies as before.
install_core_copy() {
  if [ ! -f "$SRC_ROOT/core/uv.lock" ]; then
    log "no core/uv.lock in this checkout: resolving the dependencies"
    "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" "$SRC_ROOT/core" >&2
    return 0
  fi
  REQS=$(mktemp "${TMPDIR:-/tmp}/k3code-reqs.XXXXXX")
  "$UV" export --quiet --project "$SRC_ROOT/core" --locked --no-dev --no-hashes --no-emit-project -o "$REQS" >/dev/null ||
    die "could not export the locked runtime dependencies (core/uv.lock is stale?): run 'uv lock' in core/"
  "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" -r "$REQS" >&2
  "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" --no-deps "$SRC_ROOT/core" >&2
  rm -f "$REQS"
  REQS=""
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
    # the stub answers --version, and fails on `doctor` so the tests show that presetup ignores the doctor's status
    # shellcheck disable=SC2016 # the $1 belongs to the stub script, not to this shell
    printf '#!/bin/sh\ncase "$1" in\ndoctor) echo "! browser: stub (K3_STUB_VENV) data=$K3CODE_DATA"; exit 1 ;;\n*) echo "k3code %s" ;;\nesac\n' "$VER" \
      >"$VERDIR/venv/bin/k3code"
    chmod +x "$VERDIR/venv/bin/k3code"
  else
    "$UV" venv --quiet --python '>=3.12' "$VERDIR/venv" >&2 ||
      die "could not create a Python 3.12+ environment; try: $(hint_cmd python)"
    if [ "${K3_SKIP_PIP:-0}" != 1 ]; then
      log "installing the k3code core"
      if [ "${K3_EDITABLE:-0}" = 1 ]; then
        "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" -e "$SRC_ROOT/core" >&2
      else
        install_core_copy
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
  if [ "$FROM" = git ]; then printf '%s\n' "$REF" >"$VERDIR/.ref"; fi
  printf '%s\n' "$VER" >"$VERDIR/.complete"
  BUILDING=""
  return 0
}

# ---- activation ------------------------------------------------------------
link_bin() { # link_bin NAME TARGET: BIN/NAME -> TARGET; drops a stale link of ours; never replaces someone else's
  lb=$(readlink "$BIN/$1" 2>/dev/null || true)
  if [ -e "$2" ]; then
    if [ "$lb" = "$2" ]; then return 0; fi
    if [ -e "$BIN/$1" ] || [ -L "$BIN/$1" ]; then
      case "$lb" in
        "$DATA"/*) ;;
        *)
          if [ "$FORCE" != 1 ]; then
            log "WARNING: $BIN/$1 is not a link into $DATA (another program?): left in place; --force replaces it"
            return 0
          fi
          rm -f "$BIN/$1"
          ;;
      esac
    fi
    ln -sfn "$2" "$BIN/$1"
  else
    case "$lb" in
      "$DATA"/*) rm -f "$BIN/$1" ;;
    esac
  fi
  return 0
}

set_current() { # set_current DIR: point DATA/current at DIR with one rename, so no reader ever sees it missing
  nl="$DATA/.current.$$"
  rm -f "$nl"
  ln -s "$1" "$nl"
  if mv -T "$nl" "$DATA/current" 2>/dev/null; then return 0; fi # GNU, busybox
  if [ -L "$nl" ] && mv -h "$nl" "$DATA/current" 2>/dev/null; then return 0; fi # macOS, BSD: -h does not follow
  rm -f "$nl"
  ln -sfn "$1" "$DATA/current"
}

prune_versions() { # keep the current and the previous version, and the one the running daemon executes from
  prev=$(cat "$DATA/previous" 2>/dev/null || true)
  busy=$(daemon_version)
  for d in "$DATA"/versions/*; do
    [ -d "$d" ] || continue
    n=$(basename "$d")
    case "$n" in "$VER" | "$prev") continue ;; esac
    if [ -n "$busy" ] && [ "$n" = "$busy" ]; then
      log "kept old version $n: the running k3code daemon still executes from it"
      continue
    fi
    rm -rf "$d"
    log "removed old version $n"
  done
  return 0
}

# The daemon is a systemd user unit only when `k3code service install` wrote one (the path uninstall.sh uses too).
daemon_unit() { [ "$PLATFORM" = Linux ] && have systemctl && [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/k3code.service" ]; }

daemon_version() { # the versions/<name> the running k3code daemon executes from; empty when none or unknown
  daemon_unit || return 0
  pid=$(systemctl --user show -p MainPID --value k3code.service 2>/dev/null || true)
  case "$pid" in '' | 0 | *[!0-9]*) return 0 ;; esac
  [ -d "/proc/$pid" ] || return 0
  real=$(cd "$DATA" 2>/dev/null && pwd -P) || real=$DATA
  # a venv python's exe is the base interpreter, so the version shows in its cwd or command line (the shebang path)
  { readlink "/proc/$pid/exe" && readlink "/proc/$pid/cwd" && tr '\0' '\n' <"/proc/$pid/cmdline"; } 2>/dev/null |
    while IFS= read -r p; do
      case "$p" in
        "$DATA"/versions/*) p=${p#"$DATA"/versions/} ;;
        "$real"/versions/*) p=${p#"$real"/versions/} ;;
        *) continue ;;
      esac
      printf '%s\n' "${p%%/*}"
      break
    done
}

restart_daemon() { # a running daemon keeps executing the old version until it restarts
  daemon_unit || return 0
  systemctl --user is-active --quiet k3code.service 2>/dev/null || return 0
  systemctl --user reset-failed k3code.service 2>/dev/null || true
  if systemctl --user restart k3code.service >&2; then
    log "restarted the k3code daemon (k3code.service) on $VER"
  else
    log "WARNING: could not restart k3code.service; it still runs the old version: systemctl --user restart k3code.service"
  fi
  return 0
}

activate() {
  cur=""
  if [ -L "$DATA/current" ]; then cur=$(basename "$(readlink "$DATA/current")"); fi
  if [ "$cur" != "$VER" ]; then
    if [ -n "$cur" ]; then printf '%s\n' "$cur" >"$DATA/previous"; fi
    set_current "$VERDIR"
    log "current -> $VER${cur:+ (previous: $cur, kept for rollback)}"
  fi
  if [ "$FROM" = git ] && [ -e "$DATA/source_path" ]; then rm -f "$DATA/source_path"; fi
  link_bin k3code "$DATA/current/venv/bin/k3code"
  link_bin k3 "$DATA/current/bin/k3"
  if [ "$cur" != "$VER" ]; then restart_daemon; fi
  prune_versions
}

import_bundle() {
  [ -f "$BUNDLE" ] || die "bundle not found: $BUNDLE"
  log "importing $BUNDLE (settings and sessions; secrets are not in bundles)"
  "$DATA/current/venv/bin/k3code" import "$BUNDLE" --yes >&2 || die "import of $BUNDLE failed"
}

# ---- presetup ----------------------------------------------------------------
# Optional extras after activation (--minimal skips them all). Never fails the install: each step runs in its own
# `||` context, so a failing step is reported and the next one still runs. Every step is safe to re-run: it
# writes a marker only when it did something, so a second run changes nothing.
presetup() {
  log "presetup (optional; --minimal skips it):"
  presetup_step sandbox presetup_sandbox
  presetup_step chromium presetup_chromium
  presetup_step doctor presetup_doctor
  return 0
}

presetup_step() { # presetup_step NAME FUNC
  "$2" || log "presetup: $1 step did not finish; continuing"
  return 0
}

# Same probe as k3code's sandbox.usable(): bwrap has to run a command inside its namespaces.
bwrap_cmd() { printf '%s' "${K3_BWRAP:-bwrap}"; }
bwrap_usable() {
  b=$(command -v "$(bwrap_cmd)" 2>/dev/null) || return 1
  if have timeout; then
    timeout 10 "$b" --die-with-parent --unshare-pid --ro-bind / / --dev /dev --proc /proc /bin/true >/dev/null 2>&1
  else
    "$b" --die-with-parent --unshare-pid --ro-bind / / --dev /dev --proc /proc /bin/true >/dev/null 2>&1
  fi
}

# Never runs sudo unattended: the install command is printed, and run only after a yes on a terminal.
presetup_sandbox() {
  if [ "$PLATFORM" != Linux ]; then return 0; fi
  if bwrap_usable; then
    log "presetup: sandbox ok (bubblewrap works here)"
    return 0
  fi
  if have "$(bwrap_cmd)"; then
    log "presetup: WARNING: bubblewrap is installed but cannot create a sandbox here (user namespaces blocked?)"
    say "      unattended runs (auto, yolo, background) are not sandboxed until this is fixed"
    return 0
  fi
  log "presetup: WARNING: bubblewrap is not installed; unattended runs (auto, yolo, background) are not sandboxed"
  cmd=$(hint_cmd bwrap)
  say "      install it:  $cmd"
  case "$cmd" in
    "sudo "*)
      if ask_tty "Run that now? (sudo asks for your password)"; then
        if sh -c "$cmd" </dev/tty >/dev/tty 2>&1 && bwrap_usable; then
          log "presetup: sandbox ok (bubblewrap installed)"
        else
          log "presetup: bubblewrap still not usable; run the command above by hand"
        fi
      fi
      ;;
  esac
  return 0
}
# Chromium for the browser tool: the headless shell only (about 115 MiB download plus ffmpeg, about 266 MB on disk).
# The full browser would add about 196 MB. It lives under $DATA/browsers, outside versions/, so an update keeps it.
PLAYWRIGHT_VERSION=1.63.0
chromium_present() { # the marker from an earlier run and the browser it names are both still there
  [ -f "$DATA/presetup/chromium-$PLAYWRIGHT_VERSION" ] || return 1
  if [ "${K3_STUB_VENV:-0}" = 1 ]; then return 0; fi
  # the Playwright package lives in the version's venv: a new version without it repeats only the cheap pip step
  "$VERDIR/venv/bin/python" -c "import playwright" >/dev/null 2>&1 || return 1
  for d in "$DATA"/browsers/chromium_headless_shell-*; do
    [ -d "$d" ] && return 0
  done
  return 1
}

presetup_chromium() {
  if [ "${K3CODE_SKIP_CHROMIUM:-0}" = 1 ]; then
    log "presetup: Chromium skipped (K3CODE_SKIP_CHROMIUM=1)"
    return 0
  fi
  if chromium_present; then
    log "presetup: Chromium already installed in $DATA/browsers"
    return 0
  fi
  if [ "${K3_STUB_VENV:-0}" = 1 ]; then # tests: nothing is downloaded; the marker records the decision
    mkdir -p "$DATA/presetup"
    printf 'stub\n' >"$DATA/presetup/chromium-$PLAYWRIGHT_VERSION"
    log "presetup: Chromium stub (K3_STUB_VENV)"
    return 0
  fi
  if [ "$NO_DEPS" = 1 ]; then
    log "presetup: Chromium not installed (--no-install-deps; it downloads about 115 MiB)"
    return 0
  fi
  log "presetup: installing Chromium for the browser tool (about 115 MiB download; K3CODE_SKIP_CHROMIUM=1 skips it)"
  if ! "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" "playwright==$PLAYWRIGHT_VERSION" >&2; then
    log "presetup: WARNING: could not install Playwright; the browser tool stays off"
    return 0
  fi
  if ! PLAYWRIGHT_BROWSERS_PATH="$DATA/browsers" "$VERDIR/venv/bin/python" -m playwright install --only-shell chromium >&2; then
    log "presetup: WARNING: Chromium did not install (output above); run the installer again to retry"
    return 0
  fi
  mkdir -p "$DATA/presetup"
  printf '%s\n' "$PLAYWRIGHT_VERSION" >"$DATA/presetup/chromium-$PLAYWRIGHT_VERSION"
  log "presetup: Chromium installed in $DATA/browsers"
  return 0
}
presetup_doctor() {
  if [ ! -x "$DATA/current/venv/bin/k3code" ]; then
    log "presetup: health subset skipped (k3code is not installed)"
    return 0
  fi
  log "presetup: health subset (warnings only; 'k3code doctor' gives the full report)"
  out=$(K3CODE_DATA="$DATA" "$DATA/current/venv/bin/k3code" doctor --install 2>/dev/null) || true
  if [ -n "$out" ]; then say "$out"; fi
  return 0
}

# ---- main ------------------------------------------------------------------
main() {
  export GIT_TERMINAL_PROMPT=0
  FROM="" GIT_URL="" REF="" CHANNEL=stable WANT_VERSION="" PREFIX="$HOME/.local" BUNDLE=""
  YES=0 NO_DEPS=0 CHECK=0 ACTIVATE=1 PRINT_VERSION=0 PRESETUP=1 ALLOW_ROOT=0 FORCE=0
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
      --yes | -y) YES=1 ;; # older callers; nothing is asked, and presetup never prompts for sudo
      --no-install-deps) NO_DEPS=1 ;;
      --minimal) PRESETUP=0 ;;
      --check) CHECK=1 ;;
      --allow-root) ALLOW_ROOT=1 ;;
      --force) FORCE=1 ;;
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
  case "$PREFIX" in /*) ;; *) PREFIX="$(pwd)/$PREFIX" ;; esac # links must not be relative to the cwd
  DATA="${K3CODE_DATA:-$PREFIX/share/k3code}"
  BIN="${K3_BIN_DIR:-$PREFIX/bin}"
  case "$DATA" in /*) ;; *) DATA="$(pwd)/$DATA" ;; esac
  case "$BIN" in /*) ;; *) BIN="$(pwd)/$BIN" ;; esac
  trap cleanup EXIT
  trap 'exit 1' INT TERM
  detect_platform
  detect_pm
  if [ "$CHECK" = 1 ]; then
    report
    exit 0
  fi
  check_user
  mkdir -p "$DATA" "$BIN"
  take_lock
  INSTALL_LOG="${K3_INSTALL_LOG:-$DATA/install.log}"
  printf '\n==== %s install start (args: %s) ====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$INSTALL_LOG" 2>/dev/null || true
  report
  if [ "$NO_DEPS" = 1 ]; then export UV_PYTHON_DOWNLOADS=never; fi
  if [ "$FROM" = git ] && ! have git; then die "git is needed for --from-git: $(hint_cmd git)"; fi
  ensure_uv
  if [ -z "$PY_FOUND" ] && [ "$NO_DEPS" != 1 ]; then
    log "no Python 3.12+ here: uv will download a managed one (kept in uv's own data directory)"
  fi
  ensure_bwrap
  acquire_source
  if [ ! -f "$VERDIR/.complete" ]; then
    ensure_node
    ensure_go
  fi
  install_version
  if [ "$ACTIVATE" = 1 ]; then activate; fi
  if [ "$PRINT_VERSION" = 1 ]; then
    printf '%s\n' "$VER"
    exit 0
  fi
  if [ "$ACTIVATE" != 1 ]; then exit 0; fi
  if [ -n "$BUNDLE" ]; then import_bundle; fi
  if [ "$PRESETUP" = 1 ]; then presetup || log "presetup did not finish; k3code is installed and works without it"; fi
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *)
      case "${SHELL:-}" in
        */zsh) say "Add $BIN to your PATH:  echo 'export PATH=\"$BIN:\$PATH\"' >>~/.zshrc" ;;
        */fish) say "Add $BIN to your PATH:  fish_add_path $BIN" ;;
        *) say "Add $BIN to your PATH:  echo 'export PATH=\"$BIN:\$PATH\"' >>~/.bashrc" ;;
      esac
      ;;
  esac
  if [ "$DATA" != "$HOME/.local/share/k3code" ]; then
    say "For a non-default prefix, 'k3code update' needs:  export K3CODE_DATA=\"$DATA\""
  fi
  # shellcheck disable=SC2016 # the backticks are literal text for the user
  say "$(printf 'Installed k3code %s. Next: run `k3code onboard` to set up your provider (or `k3code` to start).' "$VER")"
}

main "$@" </dev/null
