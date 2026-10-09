#!/usr/bin/env bash
# kill -9 mid-bash, resume, verify INTERRUPTED shown to the model and the command not re-run.
# shellcheck source=scripts/chaos/_common.sh
source "$(dirname "$0")/_common.sh"
MARK="$WORK/proj/marker.txt"
PROMPT='Run exactly one bash command, then tell me what happened: RUN[echo run >> marker.txt; sleep 20; echo finished >> marker.txt]'
k3code -p "$PROMPT" --session killme --permission yolo >"$WORK/out1.txt" 2>"$WORK/run1.log" &
PID=$!
for _ in $(seq 120); do [ -e "$MARK" ] && break; sleep 0.5; done
[ -e "$MARK" ] || { echo "FAIL: bash never started"; tail -5 "$WORK/run1.log"; exit 1; }
pkill -9 -P "$PID" 2>/dev/null || true
kill -9 "$PID"; wait "$PID" 2>/dev/null || true
pkill -9 -f "sleep 20" 2>/dev/null || true
echo "[chaos] killed -9 mid-bash; marker: $(tr '\n' ' ' < "$MARK")"
JOURNAL="$K3CODE_HOME/journal/killme.jsonl"
grep -c '"intent"' "$JOURNAL" | sed 's/^/intents: /'; grep -c '"done"' "$JOURNAL" | sed 's/^/dones: /' || true
k3code -p "continue: report what happened to the earlier bash command" --session killme --resume --permission yolo >"$WORK/out2.txt" 2>"$WORK/run2.log" || true
ok=1
grep -q "reliability.interrupted_tool" "$WORK/run2.log" || { echo "FAIL: no interrupted_tool event"; ok=0; }
[ "$(grep -c '^run$' "$MARK")" = 1 ] || { echo "FAIL: bash re-run (marker: $(tr '\n' ' ' < "$MARK"))"; ok=0; }
grep -q "^finished$" "$MARK" && { echo "FAIL: command completed after kill"; ok=0; }
grep -q "INTERRUPTED" "$K3CODE_HOME/journal/killme.messages.json" || { echo "FAIL: INTERRUPTED not in transcript"; ok=0; }
[ "$ok" = 1 ] && echo "PASS kill_during_bash" || exit 1
