#!/usr/bin/env bash
# Run every exit check (m0..m6 + soak) and write docs/reports/exit-status.md.
# Usage: scripts/exit/run_all.sh [--soak-minutes N] [--only m0,m1,...]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SOAK_MIN=30; ONLY="m0,m1,m2,m3,m4,m5,m6"
while [ $# -gt 0 ]; do case "$1" in
  --soak-minutes) SOAK_MIN="$2"; shift 2;; --only) ONLY="$2"; shift 2;; *) echo "bad arg $1"; exit 2;; esac; done
export EXIT_ROWS="$REPO/scripts/exit/rows/rows.jsonl"; mkdir -p "$REPO/scripts/exit/rows" "$REPO/docs/reports"
LOGS="$REPO/scripts/exit/rows/logs"; mkdir -p "$LOGS"
: > "$EXIT_ROWS"
export EXIT_RUN_START; EXIT_RUN_START="$(date +%s)"
export EXIT_SOAK_MINUTES="$SOAK_MIN"
echo "[exit] run start $(date -Is); only=$ONLY; soak=${SOAK_MIN}min"
# the soak runs in the background alongside the other checks (it is wall-clock bound)
SOAK_PID=""
if [[ ",$ONLY," == *",m2,"* ]]; then
  bash "$HERE/soak.sh" --minutes "$SOAK_MIN" --report >"$LOGS/soak.log" 2>&1 & SOAK_PID=$!
fi
for m in ${ONLY//,/ }; do
  f="$(ls "$HERE"/${m}_*.sh "$HERE"/${m}_*.py 2>/dev/null | head -1)"
  [ -z "$f" ] && { echo "[exit] no check for $m"; continue; }
  echo "[exit] === $m ($(basename "$f")) $(date +%T)"
  if [[ "$f" == *.py ]]; then python3 "$f" >"$LOGS/$m.log" 2>&1; else bash "$f" >"$LOGS/$m.log" 2>&1; fi
  echo "[exit] $m rc=$? (log: scripts/exit/rows/logs/$m.log)"
done
[ -n "$SOAK_PID" ] && { echo "[exit] waiting for soak ($SOAK_PID)"; wait "$SOAK_PID"; }
python3 "$HERE/render.py" "$EXIT_ROWS" "$REPO/docs/reports/exit-status.md"
echo "[exit] wrote docs/reports/exit-status.md"
