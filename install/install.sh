#!/bin/sh
# k3code installer: POSIX sh, idempotent, no root.
#
#   curl -fsSL <raw url>/install/install.sh | sh            # release install (private repo: needs GITHUB_TOKEN)
#   sh install/install.sh --from-source                      # from a cloned checkout (always works)
#
# Private repo: release downloads need a GitHub token. Export GITHUB_TOKEN=<token with repo read>
# or log in with `gh auth login` (the script falls back to `gh auth token`). Fetching this script via
# curl from a private repo needs the same token:
#   curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github.raw" \
#     https://api.github.com/repos/K3NOXOFFICIAL/k3code/contents/install/install.sh | sh -s -- --yes
#
# Layout:  $K3CODE_DATA (default ~/.local/share/k3code)
#            versions/<ver>/{venv,tui,bin/k3}   current -> versions/<ver>   previous (file)   node/<ver>
#          ~/.local/bin/{k3code,k3} -> symlinks into current/
#
# Flags: --from-source --from-bundle FILE --channel stable|dev --version X --yes --headless --no-setup
#        --no-activate (stage only; used by `k3code update --from-source`) --print-version --help
# Dev env: K3_EDITABLE=1 (--from-source: editable core install instead of a copy)
# Test/offline env: K3_SKIP_PIP=1 K3_SKIP_TUI=1 K3_SKIP_GO=1 K3_NO_DOWNLOAD=1 K3_STUB_VENV=1
set -eu

REPO="${K3_REPO:-K3NOXOFFICIAL/k3code}"
DATA="${K3CODE_DATA:-$HOME/.local/share/k3code}"
BIN="${K3_BIN_DIR:-$HOME/.local/bin}"
NODE_VER="${K3_NODE_VERSION:-22.23.1}"
GO_VER="${K3_GO_VERSION:-1.26.6}"

FROM_SOURCE=0 BUNDLE="" CHANNEL=stable WANT_VERSION="" YES=0 SETUP=1 ACTIVATE=1 PRINT_VERSION=0

# Install log: everything `log` prints is also appended here, and a failure prints where to look.
INSTALL_LOG="${K3_INSTALL_LOG:-$DATA/install.log}"
mkdir -p "$(dirname "$INSTALL_LOG")" 2>/dev/null || true
printf '\n==== %s install start (args: %s) ====\n' "$(date '+%F %T')" "$*" >>"$INSTALL_LOG" 2>/dev/null || true
log() {
  printf '%s\n' "k3code-install: $*" >&2
  printf '%s\n' "k3code-install: $*" >>"$INSTALL_LOG" 2>/dev/null || true
}
on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "k3code-install: FAILED (exit $rc). Log: $INSTALL_LOG" >&2
    printf '%s\n' "==== failed with exit $rc ====" >>"$INSTALL_LOG" 2>/dev/null || true
  fi
}
trap on_exit EXIT
die() { log "ERROR: $*"; exit 1; }

usage() { sed -n '2,20p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --from-source) FROM_SOURCE=1 ;;
    --from-bundle) [ $# -ge 2 ] || die "--from-bundle needs a file"; BUNDLE="$2"; shift ;;
    --channel) [ $# -ge 2 ] || die "--channel needs a value"; CHANNEL="$2"; shift ;;
    --version) [ $# -ge 2 ] || die "--version needs a value"; WANT_VERSION="${2#v}"; shift ;;
    --yes|-y) YES=1 ;;
    --headless|--no-setup) SETUP=0 ;;
    --no-activate) ACTIVATE=0 ;;
    --print-version) PRINT_VERSION=1 ;;
    -h|--help) usage ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done
case "$CHANNEL" in stable|dev) ;; *) die "--channel must be stable or dev" ;; esac

# ---- helpers ---------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

ask() { # ask "question" -> 0 if yes
  [ "$YES" = 1 ] && return 0
  if has_tty; then
    printf '%s [y/N] ' "$1" >/dev/tty
    read -r ans </dev/tty || return 1
    case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
  fi
  die "$1 -- cannot ask (no tty); re-run with --yes"
}

has_tty() { (: </dev/tty) 2>/dev/null; }
no_download() { [ "${K3_NO_DOWNLOAD:-0}" = 1 ]; }

fetch() { # fetch URL DEST
  if have curl; then curl -fsSL "$1" -o "$2"; elif have wget; then wget -qO "$2" "$1"; else die "need curl or wget"; fi
}

sha256_of() { if have sha256sum; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

OS=$(uname -s); ARCH=$(uname -m)
case "$OS" in
  Linux) GOOS=linux; NODEOS=linux ;;
  Darwin) GOOS=darwin; NODEOS=darwin; log "macOS support is best-effort" ;;
  *) die "unsupported OS: $OS" ;;
esac
case "$ARCH" in
  x86_64|amd64) GOARCH=amd64; NODEARCH=x64 ;;
  aarch64|arm64) GOARCH=arm64; NODEARCH=arm64 ;;
  *) die "unsupported architecture: $ARCH" ;;
esac

SRC_ROOT=""
if [ "$FROM_SOURCE" = 1 ]; then
  d=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || d=""
  if [ -n "$d" ] && [ -f "$d/core/pyproject.toml" ] && [ -f "$d/VERSION" ]; then SRC_ROOT="$d"
  elif [ -f ./core/pyproject.toml ] && [ -f ./VERSION ]; then SRC_ROOT=$(pwd)
  else die "--from-source must run from a k3code checkout (sh install/install.sh --from-source)"; fi
fi

PATH_ORIG="$PATH"
mkdir -p "$DATA" "$BIN"
export PATH="$BIN:$PATH"

# ---- 1. uv -----------------------------------------------------------------
UV=""
ensure_uv() {
  if have uv; then UV=$(command -v uv); return; fi
  [ -x "$BIN/uv" ] && { UV="$BIN/uv"; return; }
  no_download && die "uv is missing and K3_NO_DOWNLOAD=1"
  ask "uv (Python package manager) is missing. Install it user-locally from astral.sh?" || die "uv is required"
  log "installing uv via the official installer"
  tmp=$(mktemp "${TMPDIR:-/tmp}/uv-install.XXXXXX")
  fetch https://astral.sh/uv/install.sh "$tmp"
  UV_NO_MODIFY_PATH=1 UV_INSTALL_DIR="$BIN" sh "$tmp" >&2
  rm -f "$tmp"
  UV="$BIN/uv"; [ -x "$UV" ] || die "uv installation failed"
}

# ---- 2. Node >= 22 ---------------------------------------------------------
node_major() { "$1" --version 2>/dev/null | sed 's/^v//; s/\..*//'; }
ensure_node() {
  if have node && [ "$(node_major node)" -ge 22 ] 2>/dev/null; then return; fi
  for n in "$DATA"/node/*/bin/node; do
    if [ -x "$n" ] && [ "$(node_major "$n")" -ge 22 ] 2>/dev/null; then PATH="$(dirname "$n"):$PATH"; export PATH; return; fi
  done
  no_download && die "Node >= 22 is missing and K3_NO_DOWNLOAD=1"
  log "Node >= 22 not found; downloading Node $NODE_VER into $DATA/node/$NODE_VER"
  base="https://nodejs.org/dist/v$NODE_VER"
  name="node-v$NODE_VER-$NODEOS-$NODEARCH"
  ext=tar.xz; [ "$NODEOS" = darwin ] && ext=tar.gz
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/k3-node.XXXXXX")
  fetch "$base/$name.$ext" "$tmp/$name.$ext"
  fetch "$base/SHASUMS256.txt" "$tmp/SHASUMS256.txt"
  want=$(grep " $name.$ext\$" "$tmp/SHASUMS256.txt" | cut -d' ' -f1)
  [ -n "$want" ] && [ "$want" = "$(sha256_of "$tmp/$name.$ext")" ] || die "Node checksum mismatch"
  mkdir -p "$DATA/node/$NODE_VER"
  tar -xf "$tmp/$name.$ext" -C "$DATA/node/$NODE_VER" --strip-components=1
  rm -rf "$tmp"
  PATH="$DATA/node/$NODE_VER/bin:$PATH"; export PATH
}

# ---- 3. Go (source builds only) -------------------------------------------
ensure_go() {
  [ "${K3_SKIP_GO:-0}" = 1 ] && return
  if have go; then return; fi
  [ -x "$DATA/go/$GO_VER/bin/go" ] && { PATH="$DATA/go/$GO_VER/bin:$PATH"; export PATH; return; }
  no_download && die "Go is missing and K3_NO_DOWNLOAD=1"
  log "Go not found; downloading Go $GO_VER into $DATA/go/$GO_VER"
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/k3-go.XXXXXX")
  fetch "https://go.dev/dl/go$GO_VER.$GOOS-$GOARCH.tar.gz" "$tmp/go.tgz"
  mkdir -p "$DATA/go/$GO_VER"
  tar -xzf "$tmp/go.tgz" -C "$DATA/go/$GO_VER" --strip-components=1
  rm -rf "$tmp"
  PATH="$DATA/go/$GO_VER/bin:$PATH"; export PATH
}

# ---- release lookup (private repo: token) ---------------------------------
gh_token() {
  if [ -n "${GITHUB_TOKEN:-}" ]; then printf '%s' "$GITHUB_TOKEN"; return; fi
  if [ -n "${GH_TOKEN:-}" ]; then printf '%s' "$GH_TOKEN"; return; fi
  if [ "${K3_NO_GH:-0}" != 1 ] && have gh; then gh auth token 2>/dev/null || true; fi
}

# Downloads the release assets for this arch into $1 and prints the version. Runs under uv's python.
FETCH_PY='
import hashlib, json, os, platform, sys, urllib.request
repo, channel, want, dest = sys.argv[1:5]
tok = os.environ.get("GITHUB_TOKEN", "")
def req(url, accept="application/vnd.github+json"):
    r = urllib.request.Request(url, headers={"Accept": accept, "Authorization": "Bearer " + tok, "X-GitHub-Api-Version": "2022-11-28"})
    return urllib.request.urlopen(r, timeout=120)
api = "https://api.github.com/repos/" + repo + "/releases"
if want:
    rel = json.load(req(api + "/tags/v" + want))
elif channel == "stable":
    rel = json.load(req(api + "/latest"))
else:
    rel = json.load(req(api + "?per_page=1"))[0]
ver = rel["tag_name"].lstrip("v")
arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), platform.machine())
wanted = ("SHA256SUMS",)
for a in rel["assets"]:
    n = a["name"]
    if n in wanted or n.endswith(".whl") or n.startswith("k3code-tui") or n == "k3-linux-" + arch:
        with req(a["url"], "application/octet-stream") as r, open(os.path.join(dest, n), "wb") as f:
            f.write(r.read())
sums = {}
p = os.path.join(dest, "SHA256SUMS")
if os.path.exists(p):
    for line in open(p):
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0]
for n in os.listdir(dest):
    if n != "SHA256SUMS" and n in sums and hashlib.sha256(open(os.path.join(dest, n), "rb").read()).hexdigest() != sums[n]:
        sys.exit("checksum mismatch: " + n)
print(ver)
'

# ---- main ------------------------------------------------------------------
ensure_uv
[ "${K3_SKIP_TUI:-0}" = 1 ] || ensure_node
[ "$FROM_SOURCE" = 1 ] && ensure_go

DL=""
if [ "$FROM_SOURCE" = 1 ]; then
  VER=$(tr -d '[:space:]' <"$SRC_ROOT/VERSION")
  sha=$(git -C "$SRC_ROOT" rev-parse --short HEAD 2>/dev/null || true)
  VER="$VER-src${sha:+.$sha}"
else
  TOKEN=$(gh_token)
  [ -n "$TOKEN" ] || die "the repo is private: set GITHUB_TOKEN (repo read access) or run \`gh auth login\`. Or use --from-source from a clone."
  DL=$(mktemp -d "${TMPDIR:-/tmp}/k3-release.XXXXXX")
  log "looking up release (channel=$CHANNEL${WANT_VERSION:+, version=$WANT_VERSION})"
  VER=$(GITHUB_TOKEN="$TOKEN" "$UV" run --no-project --python '>=3.12' python -I -c "$FETCH_PY" "$REPO" "$CHANNEL" "$WANT_VERSION" "$DL" | tail -n 1) \
    || die "release lookup/download failed (token valid? release exists?)"
fi

VERDIR="$DATA/versions/$VER"
if [ -f "$VERDIR/.complete" ]; then
  log "version $VER already installed"
else
  log "installing version $VER into $VERDIR"
  rm -rf "$VERDIR"; mkdir -p "$VERDIR"
  trap 'on_exit; rm -rf "$VERDIR" ; [ -z "$DL" ] || rm -rf "$DL"' EXIT INT TERM
  if [ "${K3_STUB_VENV:-0}" = 1 ]; then # tests: fake core, no pip
    mkdir -p "$VERDIR/venv/bin"
    printf '#!/bin/sh\necho "k3code %s"\n' "$VER" >"$VERDIR/venv/bin/k3code"; chmod +x "$VERDIR/venv/bin/k3code"
  else
    "$UV" venv --quiet --python '>=3.12' "$VERDIR/venv" >&2
    if [ "$FROM_SOURCE" = 1 ]; then
      if [ "${K3_SKIP_PIP:-0}" != 1 ]; then
        # A regular install by default: the installed daemon must not import a git checkout that can be edited,
        # switched to another branch or deleted (a removed worktree used to break `k3code` outright).
        # K3_EDITABLE=1 keeps the developer's `pip install -e` (changes show up without re-installing).
        if [ "${K3_EDITABLE:-0}" = 1 ]; then
          "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" -e "$SRC_ROOT/core" >&2
        else
          "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" "$SRC_ROOT/core" >&2
        fi
      fi
    else
      "$UV" pip install --quiet --python "$VERDIR/venv/bin/python" "$DL"/*.whl >&2
    fi
  fi
  mkdir -p "$VERDIR/tui" "$VERDIR/bin"
  if [ "$FROM_SOURCE" = 1 ]; then
    if [ "${K3_SKIP_TUI:-0}" != 1 ]; then
      log "building the TUI"
      (cd "$SRC_ROOT/tui" && npm ci --no-audit --no-fund >&2 && npm run build:ink >&2 && npm run build >&2)
      cp -R "$SRC_ROOT/tui/dist" "$VERDIR/tui/dist"
    fi
    if [ "${K3_SKIP_GO:-0}" != 1 ]; then
      log "building the k3 pane binary"
      (cd "$SRC_ROOT/panes" && go build -o "$VERDIR/bin/k3" ./cmd/k3 >&2)
    fi
    printf '%s\n' "$SRC_ROOT" >"$DATA/source_path"
  else
    tui=$(ls "$DL"/k3code-tui*.tar.gz 2>/dev/null | head -n 1 || true)
    [ -z "$tui" ] || tar -xzf "$tui" -C "$VERDIR/tui"
    if [ -f "$DL/k3-linux-$GOARCH" ]; then cp "$DL/k3-linux-$GOARCH" "$VERDIR/bin/k3"; chmod +x "$VERDIR/bin/k3"; fi
    rm -rf "$DL"
  fi
  printf '%s\n' "$VER" >"$VERDIR/.complete"
  trap on_exit EXIT
  trap - INT TERM
fi

if [ "$ACTIVATE" = 1 ]; then
  cur=""
  [ -L "$DATA/current" ] && cur=$(basename "$(readlink "$DATA/current")")
  if [ "$cur" != "$VER" ]; then
    [ -z "$cur" ] || printf '%s\n' "$cur" >"$DATA/previous"
    ln -sfn "$VERDIR" "$DATA/current"
    log "current -> $VER"
  fi
  link() { # link NAME TARGET
    [ -e "$2" ] || return 0
    [ "$(readlink "$BIN/$1" 2>/dev/null || true)" = "$2" ] || ln -sfn "$2" "$BIN/$1"
  }
  link k3code "$DATA/current/venv/bin/k3code"
  link k3 "$DATA/current/bin/k3"
fi

if [ "$PRINT_VERSION" = 1 ]; then printf '%s\n' "$VER"; exit 0; fi
[ "$ACTIVATE" = 1 ] || exit 0

case ":$PATH_ORIG:" in *":$BIN:"*) ;; *) log "note: add $BIN to your PATH (e.g. export PATH=\"$BIN:\$PATH\")" ;; esac

# ---- doctor / import / setup ----------------------------------------------
K3="$BIN/k3code"
log "running k3code doctor"
K3CODE_DATA="$DATA" "$K3" doctor --no-probe >&2 || log "doctor reported problems (expected before setup)"

if [ -n "$BUNDLE" ]; then
  [ -f "$BUNDLE" ] || die "bundle not found: $BUNDLE"
  log "importing bundle $BUNDLE"
  K3CODE_DATA="$DATA" "$K3" import "$BUNDLE" --yes >&2
fi
if [ "$SETUP" = 1 ]; then
  if has_tty; then
    if [ -n "$BUNDLE" ]; then K3CODE_DATA="$DATA" "$K3" setup --step secrets </dev/tty
    else K3CODE_DATA="$DATA" "$K3" setup </dev/tty; fi
  else
    log "no terminal available: run \`k3code setup\` later"
  fi
fi
log "done. Version $VER installed. Run: k3code"
