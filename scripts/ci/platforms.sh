#!/usr/bin/env bash
# Install, update, roll back and uninstall k3code on clean Linux distributions in containers, and through install.ps1
# against a WSL stand-in. This is the local replacement for the installer.yml runners: run it before a release and after
# any change to install/, update.py or service.py.
#
# Usage: scripts/ci/platforms.sh [--full] [TARGET...]
#
#   TARGET   ubuntu (24.04, what `wsl --install` sets up), ubuntu2204 (22.04 with only git and curl added: no Go,
#            no python3, no unzip; with --full the installer fetches its own Go), debian (12, /bin/sh is dash),
#            fedora (44, dnf), alpine (3.20: musl and BusyBox), wsl (install.ps1 and uninstall.ps1 run by
#            PowerShell 7, with a wsl.exe stand-in that runs each command as a normal user in an Ubuntu container).
#            Default: all six.
#   --full   also build the TUI and the k3 binary (fetches Node and Go; several minutes per target)
#
# Each target builds a throwaway clone of HEAD (commit first: uncommitted changes are not tested), installs it as a
# non-root user, makes a new commit and runs `k3code update` (from git for the distros, from the checkout for wsl,
# where the checkout belongs to another user as a Windows clone seen from WSL can), checks that `current` moved,
# rolls back, and uninstalls. Needs podman or docker; wsl also needs pwsh. Logs go to .k3dev/platforms/<time>/.
# Not covered: macOS (run `sh install/install.sh` and `k3code update` on a Mac by hand) and a real Windows machine.
set -euo pipefail

HERE=$(cd "$(dirname -- "$0")" && pwd)
K3CI_NAME=platforms.sh
# shellcheck source=scripts/ci/lib.sh
. "$HERE/lib.sh"
strip_git_env
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)

FULL=0
TARGETS=()
while [ $# -gt 0 ]; do
  case $1 in
    --full) FULL=1 ;;
    -h | --help)
      sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
      exit 0
      ;;
    ubuntu | ubuntu2204 | debian | fedora | alpine | wsl) TARGETS+=("$1") ;;
    *) die "unknown option or target: $1 (see --help)" ;;
  esac
  shift
done
[ ${#TARGETS[@]} -gt 0 ] || TARGETS=(ubuntu ubuntu2204 debian fedora alpine wsl)

ENGINE=""
for e in podman docker; do if have "$e"; then ENGINE=$e && break; fi; done
[ -n "$ENGINE" ] || die "podman or docker is needed: dnf install podman / apt install podman"

RUN=$ROOT/.k3dev/platforms/$(date +%Y%m%d-%H%M%S)
mkdir -p "$RUN"
WORK=$RUN/src
git clone -q --no-hardlinks "$ROOT" "$WORK"
git -C "$WORK" checkout -q -B Main HEAD
git -C "$WORK" config core.fileMode false # as Windows git sets it; the wsl target makes the files world-writable
note "testing $(git -C "$ROOT" rev-parse --short HEAD) (committed changes only); logs in $RUN"

SKIP_ENV="K3CODE_SKIP_CHROMIUM=1"
[ "$FULL" = 1 ] || SKIP_ENV="$SKIP_ENV K3_SKIP_TUI=1 K3_SKIP_GO=1"

# Runs as root in the container: tools, a user, then the user's part from /payload.sh.
PREPARE='set -eu
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null && apt-get install -y -qq git curl ca-certificates >/dev/null
elif command -v dnf >/dev/null; then # the Fedora image has no su (util-linux) and no git
  dnf install -y -q --setopt=install_weak_deps=False git curl ca-certificates tar gzip util-linux shadow-utils >/dev/null
elif command -v apk >/dev/null; then apk add -q git curl ca-certificates
fi
if command -v useradd >/dev/null; then useradd -m u; else adduser -D u; fi
'
# ubuntu2204 is the box whose system Go is too old or missing: nothing the preparation pulls in may bring one along.
# shellcheck disable=SC2016 # expanded by the container's shell, not this one
MINIMAL='for c in go unzip python3; do
  if command -v "$c" >/dev/null; then echo "PLATFORM CHECK FAILED: $c is preinstalled, the image is not minimal" >&2; exit 1; fi
done
'

# The user's part for a distro target: install from a bare clone, update to a new commit, roll back, uninstall.
# shellcheck disable=SC2016 # expanded by the container's shell, not this one
DISTRO_PAYLOAD='set -eu
steps="" t=$(date +%s)
step() { n=$(date +%s); steps="$steps${steps:+, }$1 $((n - t))s"; t=$n; } # step NAME: NAME took the time since the last step
fail() { echo "PLATFORM CHECK FAILED: $*${steps:+ (passed: $steps)}" >&2; exit 1; }
cd "$HOME"
git config --global user.email ci@k3code.invalid && git config --global user.name ci
git clone -q --bare /src bare.git && git clone -q bare.git work
export PATH=$HOME/.local/bin:$PATH
sh work/install/install.sh --from-git "file://$HOME/bare.git" --ref Main || fail "install"
first=$(basename "$(readlink ~/.local/share/k3code/current)")
k3code --version || fail "k3code does not start"
step install
mkdir -p ~/.k3code && printf "update:\n  url: file://%s/bare.git\n" "$HOME" >~/.k3code/config.yaml
k3code update --check || fail "update --check"
step check
(cd work && git commit -q --allow-empty -m "platform check" && git push -q origin Main)
k3code update --yes || fail "update"
second=$(basename "$(readlink ~/.local/share/k3code/current)")
[ "$second" != "$first" ] || fail "update did not switch versions ($first)"
step update
k3code update --yes | grep -q "Already up to date" || fail "a second update was not a no-op"
step no-op
k3code update --rollback || fail "rollback"
[ "$(basename "$(readlink ~/.local/share/k3code/current)")" = "$first" ] || fail "rollback did not return to $first"
step rollback
sh work/install/uninstall.sh || fail "uninstall"
[ ! -e ~/.local/share/k3code ] && [ ! -e ~/.local/bin/k3code ] || fail "uninstall left files"
step uninstall
echo "PLATFORM CHECK OK: $first -> $second -> $first ($steps)"
'

RESULTS=$RUN/results
: >"$RESULTS"
result() { printf '%s\t%s\n' "$1" "$2" >>"$RESULTS"; } # result TARGET TEXT (no associative arrays: bash 3.2)

run_distro() { # run_distro NAME IMAGE [ROOT_CHECK]: ROOT_CHECK runs as root after PREPARE
  local name=$1 image=$2 check=${3:-} log=$RUN/$1.log start=$SECONDS
  note "$name: $image"
  printf '%s' "$DISTRO_PAYLOAD" >"$RUN/$name.payload.sh"
  # shellcheck disable=SC2086 # SKIP_ENV is a list of VAR=value words
  if "$ENGINE" run --rm -v "$WORK:/src:ro,z" -v "$RUN/$name.payload.sh:/payload.sh:ro,z" "$image" sh -c \
    "$PREPARE $check git config --system --add safe.directory '*'; su u -c 'env $SKIP_ENV sh /payload.sh'" >"$log" 2>&1 &&
    grep -q "PLATFORM CHECK OK" "$log"; then
    result "$name" "ok   $(grep "PLATFORM CHECK OK" "$log" | sed 's/^PLATFORM CHECK OK: //') in $((SECONDS - start))s"
  else
    result "$name" "FAIL $(grep -m 1 "PLATFORM CHECK FAILED" "$log" | sed 's/^PLATFORM CHECK FAILED: //') (see $log)"
  fi
}

run_wsl() {
  local log=$RUN/wsl.log ctr="k3code-platforms-wsl-$$"
  note "wsl: install.ps1 with pwsh, wsl.exe stand-in over ubuntu:24.04"
  if ! have pwsh; then
    result wsl "FAIL (pwsh not found: https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-linux)"
    return 0
  fi
  trap '"$ENGINE" rm -f "$ctr" >/dev/null 2>&1 || true' EXIT
  (
    # The checkout is mounted at its own path, owned by another user inside (root), as a clone made by Windows
    # git can be when WSL sees it: that is the case `k3code update --from-source` has to handle.
    "$ENGINE" run -d --name "$ctr" -v "$WORK:$WORK:z" docker.io/library/ubuntu:24.04 sleep infinity >/dev/null
    "$ENGINE" exec "$ctr" sh -c "$PREPARE chmod -R a+rwX '$WORK'"
    # wslpath -a PATH: paths here are Linux paths already
    # shellcheck disable=SC2016 # the stub expands $2, not this shell
    printf '#!/bin/sh\nprintf "%%s\\n" "$2"\n' >"$RUN/wslpath"
    chmod 755 "$RUN/wslpath"
    "$ENGINE" cp "$RUN/wslpath" "$ctr:/usr/local/bin/wslpath"
    mkdir -p "$RUN/wsl-bin" "$RUN/appdata" "$RUN/winhome"
    cat >"$RUN/wsl-bin/wsl" <<EOF
#!/bin/sh
# wsl.exe stand-in (Windows 10 inbox WSL: no --cd): runs --exec commands as user u in $ctr
if [ "\$1" = --list ]; then printf '  NAME      STATE           VERSION\\n* Ubuntu    Running         2\\n'; exit 0; fi
while [ \$# -gt 0 ]; do
  case "\$1" in
    -d) shift ;;
    --exec) shift; break ;;
    *) echo "Invalid command line option: \$1" >&2; exit 1 ;;
  esac
  shift
done
if [ "\$1" = wslpath ]; then printf '%s\\n' "\$3"; exit 0; fi # paths here are Linux paths already
cwd=/home/u
case "\$PWD" in "$WORK"*) cwd=\$PWD ;; esac # wsl.exe starts in the current directory when WSL can see it
exec $ENGINE exec -i -u u -e HOME=/home/u $(for v in $SKIP_ENV; do printf -- '-e %s ' "$v"; done)-w "\$cwd" "$ctr" "\$@"
EOF
    chmod +x "$RUN/wsl-bin/wsl"
    local w=$RUN/wsl-bin/wsl
    export K3_WSL=$w LOCALAPPDATA=$RUN/appdata
    fail() { # stops the subshell: no later step runs on a broken install
      echo "PLATFORM CHECK FAILED: $*"
      exit 1
    }
    pwsh -NoProfile -File "$WORK/install/install.ps1" -NoModifyPath --from-source || fail "install.ps1"
    shim=$(cat "$RUN/appdata/k3code/bin/k3code.cmd")
    printf 'k3code.cmd:\n%s\n' "$shim"
    case $shim in *'wsl.exe -d Ubuntu --exec sh -lc "exec /home/u/.local/bin/k3code \"$@\"" k3code %*'*) ;; *) fail "unexpected shim" ;; esac
    # what k3code.cmd runs, from the checkout's folder
    k3() { (cd "$WORK" && "$w" -d Ubuntu --exec sh -lc 'exec /home/u/.local/bin/k3code "$@"' k3code "$@"); }
    k3 --version || fail "k3code does not start through the shim command"
    first=$(k3 update --check | sed -n 's/^current: //p')
    git -C "$WORK" -c user.email=ci@k3code.invalid -c user.name=ci commit -q --allow-empty -m "platform check"
    k3 update --from-source --no-pull --yes || fail "update --from-source"
    second=$(k3 update --check | sed -n 's/^current: //p')
    [ -n "$first" ] && [ "$second" != "$first" ] || fail "update did not switch versions ($first -> $second)"
    k3 update --from-source --no-pull --yes | grep -q "Already up to date" || fail "a second update was not a no-op"
    k3 update --rollback || fail "rollback"
    [ "$(k3 update --check | sed -n 's/^current: //p')" = "$first" ] || fail "rollback did not return to $first"
    pwsh -NoProfile -File "$WORK/install/uninstall.ps1" || fail "uninstall.ps1"
    [ ! -e "$RUN/appdata/k3code" ] || fail "uninstall.ps1 left the shims"
    "$ENGINE" exec "$ctr" test ! -e /home/u/.local/share/k3code || fail "uninstall.ps1 left the install in WSL"
    echo "PLATFORM CHECK OK: $first -> $second -> rolled back"
  ) >"$log" 2>&1 || true
  "$ENGINE" rm -f "$ctr" >/dev/null 2>&1 || true
  trap - EXIT
  if grep -q "PLATFORM CHECK OK" "$log" && ! grep -q "PLATFORM CHECK FAILED" "$log"; then
    result wsl "ok   $(grep "PLATFORM CHECK OK" "$log" | sed 's/^PLATFORM CHECK OK: //')"
  else
    result wsl "FAIL (see $log)"
  fi
}

for t in "${TARGETS[@]}"; do
  case $t in
    ubuntu) run_distro ubuntu docker.io/library/ubuntu:24.04 ;;
    ubuntu2204) run_distro ubuntu2204 docker.io/library/ubuntu:22.04 "$MINIMAL" ;;
    debian) run_distro debian docker.io/library/debian:12 ;;
    fedora) run_distro fedora registry.fedoraproject.org/fedora:44 ;;
    alpine) run_distro alpine docker.io/library/alpine:3.20 ;;
    wsl) run_wsl ;;
  esac
done

rc=0
printf '\n%-10s %s\n' target result
while IFS=$'\t' read -r t r; do
  printf '%-10s %s\n' "$t" "$r"
  case $r in ok*) ;; *) rc=1 ;; esac
done <"$RESULTS"
rm -rf "$WORK"
exit $rc
