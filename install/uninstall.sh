#!/bin/sh
# Remove a k3code install: its versions, the private Node and Go, the k3code/k3 links, the systemd unit, and what
# presetup added under the install root (the Chromium location $DATA/browsers and the $DATA/presetup markers).
# Your data (~/.k3code, ~/.config/k3code) is kept unless you pass --purge, which asks you to type yes on a terminal
# (--yes answers it; without a terminal and without --yes, --purge is refused). uv is shared, so it stays.
#   sh uninstall.sh [--prefix DIR] [--purge [--yes]]
set -eu

usage() {
  echo "usage: uninstall.sh [--prefix DIR] [--purge [--yes]]   (--purge also deletes ~/.k3code and ~/.config/k3code)"
  exit 0
}

main() {
  PREFIX="$HOME/.local"
  PURGE=0
  YES=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --purge) PURGE=1 ;;
      --yes | -y) YES=1 ;;
      --prefix)
        [ $# -ge 2 ] || {
          echo "--prefix needs a directory" >&2
          exit 1
        }
        PREFIX=$2
        shift
        ;;
      -h | --help) usage ;;
      *)
        echo "unknown option: $1" >&2
        exit 1
        ;;
    esac
    shift
  done
  case "$PREFIX" in /*) ;; *) PREFIX="$(pwd)/$PREFIX" ;; esac
  DATA="${K3CODE_DATA:-$PREFIX/share/k3code}"
  BIN="${K3_BIN_DIR:-$PREFIX/bin}"
  user_data="${K3CODE_HOME:-$HOME/.k3code} and ${XDG_CONFIG_HOME:-$HOME/.config}/k3code"
  # Decided before anything is removed: the user data holds keys and sessions that no reinstall brings back.
  if [ "$PURGE" = 1 ] && [ "$YES" != 1 ]; then
    if ! (: </dev/tty) 2>/dev/null; then
      echo "--purge deletes $user_data (keys, sessions) and needs confirmation: pass --yes, or run it on a terminal" >&2
      exit 1
    fi
    printf '%s' "--purge deletes $user_data (keys, sessions). Type yes to continue: " >/dev/tty
    read -r ans </dev/tty || ans=""
    if [ "$ans" != yes ]; then
      echo "nothing removed" >&2
      exit 1
    fi
  fi

  units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" # where `k3code service install` writes them
  unit="$units/k3code.service"
  recover="$units/k3code-recover.service"
  if [ -f "$unit" ] || [ -f "$recover" ]; then
    if [ -x "$BIN/k3code" ] && "$BIN/k3code" service uninstall; then
      :
    else # k3code is broken or gone: do what `k3code service uninstall` does
      systemctl --user disable --now k3code.service k3code-recover.service 2>/dev/null || true
      rm -f "$unit" "$recover"
      systemctl --user daemon-reload 2>/dev/null || true
    fi
    for u in "$unit" "$recover"; do
      if [ -f "$u" ]; then echo "could not remove the systemd unit; remove $u by hand" >&2; fi
    done
  fi
  for l in k3code k3; do
    t=$(readlink "$BIN/$l" 2>/dev/null || true)
    case "$t" in "$DATA"/*) rm -f "$BIN/$l" ;; esac
  done
  # Only delete a directory that really is a k3code install root.
  if [ -d "$DATA/versions" ] || [ -L "$DATA/current" ]; then
    rm -rf "$DATA"
    echo "removed $DATA"
  elif [ -e "$DATA" ]; then
    echo "$DATA does not look like a k3code install; left in place" >&2
  fi

  if [ "$PURGE" = 1 ]; then
    rm -rf "${K3CODE_HOME:-$HOME/.k3code}" "${XDG_CONFIG_HOME:-$HOME/.config}/k3code"
    echo "purged user data"
  else
    echo "kept $user_data (use --purge to delete)"
  fi
  echo "k3code uninstalled (system packages such as bubblewrap were left alone)"
}

main "$@"
