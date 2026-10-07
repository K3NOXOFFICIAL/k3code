#!/bin/sh
# Upstream sync dry run: 3-way diff of the vendored subtrees (tuios -> panes/, hermes-agent -> tui/, tui/shared,
# vendored files) against upstream HEAD, relative to the base commits recorded in VENDOR.toml / panes/UPSTREAM.toml.
# Read-only to the repo (temp dirs only), never pushes.
#   scripts/sync-upstream.sh --dry-run [--json] [--threshold N]
# Exit: 0 all subtrees under the threshold, 1 a subtree over it, 2 usage error, 3 upstream unreachable (PENDING).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
case "${1:-}" in --dry-run) ;; *) echo "usage: $0 --dry-run [--json] [--threshold N]" >&2; exit 2 ;; esac
shift
exec python3 "$HERE/sync_upstream.py" "$@"
