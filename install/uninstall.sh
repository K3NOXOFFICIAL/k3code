#!/bin/sh
# Remove the k3code install (versions, node, links). Keeps ~/.k3code and ~/.config/k3code unless --purge.
set -eu
DATA="${K3CODE_DATA:-$HOME/.local/share/k3code}"
BIN="${K3_BIN_DIR:-$HOME/.local/bin}"
PURGE=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    -h|--help) echo "usage: uninstall.sh [--purge]   (--purge also deletes ~/.k3code and ~/.config/k3code)"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done
unit="$HOME/.config/systemd/user/k3code.service"
if [ -f "$unit" ] && [ -x "$BIN/k3code" ]; then
  "$BIN/k3code" service uninstall || echo "could not remove the systemd unit; remove $unit by hand" >&2
fi
for l in k3code k3; do
  t=$(readlink "$BIN/$l" 2>/dev/null || true)
  case "$t" in "$DATA"/*) rm -f "$BIN/$l" ;; esac
done
rm -rf "$DATA"
if [ "$PURGE" = 1 ]; then
  rm -rf "${K3CODE_HOME:-$HOME/.k3code}" "${XDG_CONFIG_HOME:-$HOME/.config}/k3code"
  echo "purged user data"
else
  echo "kept ${K3CODE_HOME:-$HOME/.k3code} and ${XDG_CONFIG_HOME:-$HOME/.config}/k3code (use --purge to delete)"
fi
echo "k3code uninstalled"
