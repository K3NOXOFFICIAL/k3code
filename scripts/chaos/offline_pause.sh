#!/usr/bin/env bash
# Multi-turn headless task; the provider goes unreachable midway (proxy "down"),
# then comes back. Expect reliability.paused -> reliability.resumed and the task finishing.
# shellcheck source=scripts/chaos/_common.sh
source "$(dirname "$0")/_common.sh"
LOG="$WORK/run.log"
PROMPT='Do these steps in order, one bash tool call each, then reply DONE: RUN[echo one > a.txt] RUN[sleep 2; echo two > b.txt] RUN[echo three > c.txt] RUN[cat a.txt b.txt c.txt]'
k3code -p "$PROMPT" --session offline-pause --permission yolo >"$WORK/out.txt" 2>"$LOG" &
PID=$!
for _ in $(seq 60); do [ -e "$WORK/proj/a.txt" ] && break; sleep 0.5; done
echo down > "$MODE"; echo "[chaos] proxy DOWN"
for _ in $(seq 120); do grep -q "reliability.paused" "$LOG" && break; sleep 0.5; done
sleep 3
echo up > "$MODE"; echo "[chaos] proxy UP"
wait "$PID" && RC=0 || RC=$?
grep -E "reliability\.(paused|resumed|parked)|net\.state" "$LOG" | head -20
ok=1
grep -q "reliability.paused" "$LOG" || { echo "FAIL: no paused"; ok=0; }
grep -q "reliability.resumed" "$LOG" || { echo "FAIL: no resumed"; ok=0; }
[ "$RC" = 0 ] || { echo "FAIL: exit $RC"; tail -5 "$LOG"; ok=0; }
[ -e "$WORK/proj/c.txt" ] || { echo "FAIL: task did not finish (c.txt missing)"; ok=0; }
[ "$ok" = 1 ] && echo "PASS offline_pause" || exit 1
