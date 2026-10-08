#!/bin/sh
# Remove a k3code install: its versions, the private Node and Go, the k3code/k3 links, the systemd unit, and what
# presetup added under the install root (the Chromium location $DATA/browsers and the $DATA/presetup markers).
# Your data (~/.k3code, ~/.config/k3code) is kept unless you pass --purge. uv is shared, so it stays.
#   sh uninstall.sh [--prefix DIR] [--purge]
set -eu

usage() {
  echo "usage: uninstall.sh [--prefix DIR] [--purge]   (--purge also deletes ~/.k3code and ~/.config/k3code)"
  exit 0
}

main() {
  PREFIX="$HOME/.local"
  PURGE=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --purge) PURGE=1 ;;
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

  unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/k3code.service" # where `k3code service install` writes it
  if [ -f "$unit" ]; then
    if [ -x "$BIN/k3code" ] && "$BIN/k3code" service uninstall; then
      :
    else # k3code is broken or gone: do what `k3code service uninstall` does
      systemctl --user disable --now k3code.service 2>/dev/null || true
      rm -f "$unit"
      systemctl --user daemon-reload 2>/dev/null || true
    fi
    if [ -f "$unit" ]; then echo "could not remove the systemd unit; remove $unit by hand" >&2; fi
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
    echo "kept ${K3CODE_HOME:-$HOME/.k3code} and ${XDG_CONFIG_HOME:-$HOME/.config}/k3code (use --purge to delete)"
  fi
  echo "k3code uninstalled (system packages such as bubblewrap were left alone)"
}

main "$@"
