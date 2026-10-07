#!/usr/bin/env bash
# omni-worker.sh — run one k3code build task with a headless Claude Code worker
# whose model calls go through OmniRoute (not the Claude account).
#
# usage: scripts/dev/omni-worker.sh <task.md> <name> [base-branch] [model]
#   task.md      task spec (self-contained; see scripts/dev/tasks/)
#   name         worker name -> branch w/<name>, worktree .claude/worktrees/w-<name>
#   base-branch  branch to fork from (default: current branch of the repo root checkout)
#   model        Claude Code model alias mapped by the omniroute profile (default: sonnet)
#
# Retries until the worker reports success (bounded by K3DEV_MAX_ATTEMPTS),
# resuming the same session after API/network failures. Logs: .k3dev/runs/<name>/
set -uo pipefail

TASK=${1:?task file}; NAME=${2:?name}; BASE=${3:-}; MODEL=${4:-sonnet}
SETTINGS=${K3DEV_SETTINGS:-$HOME/.claude/settings.omniroute.json}
MAX_ATTEMPTS=${K3DEV_MAX_ATTEMPTS:-8}
REPO=$(git -C "$(dirname "$0")" rev-parse --path-format=absolute --git-common-dir | sed 's#/\.git$##')
WT="$REPO/.claude/worktrees/w-$NAME"
RUNS="$REPO/.k3dev/runs/$NAME"
PREAMBLE="$(dirname "$(readlink -f "$0")")/worker-preamble.md"
mkdir -p "$RUNS"
TASK=$(readlink -f "$TASK")

[ -f "$SETTINGS" ] || { echo "missing $SETTINGS" >&2; exit 2; }

if [ ! -d "$WT" ]; then
  BASE=${BASE:-$(git -C "$REPO" rev-parse --abbrev-ref HEAD)}
  git -C "$REPO" worktree add -q "$WT" -b "w/$NAME" "$BASE" || exit 2
fi

wait_online() {  # pause while offline / gateway unreachable, resume automatically
  local n=0
  until curl -s -o /dev/null -m 10 https://<omniroute-public-host>/; do
    [ $((n % 6)) -eq 0 ] && echo "$(date -Is) offline/gateway down — waiting" >> "$RUNS/driver.log"
    n=$((n + 1)); sleep 20
  done
}

PROMPT="$(cat "$PREAMBLE")

## Your task
$(cat "$TASK")"

SESSION=""; attempt=0; status=fail
while [ $attempt -lt "$MAX_ATTEMPTS" ]; do
  attempt=$((attempt + 1)); wait_online
  out="$RUNS/attempt-$attempt.json"
  echo "$(date -Is) attempt $attempt (session=${SESSION:-new})" >> "$RUNS/driver.log"
  if [ -z "$SESSION" ]; then
    (cd "$WT" && nice -n 10 ionice -c3 claude -p --settings "$SETTINGS" --model "$MODEL" \
       --permission-mode auto --output-format json "$PROMPT") > "$out" 2> "$RUNS/attempt-$attempt.err"
  else
    (cd "$WT" && nice -n 10 ionice -c3 claude -p --settings "$SETTINGS" --model "$MODEL" \
       --permission-mode auto --output-format json --resume "$SESSION" \
       "Continue the task from where you stopped. Re-check the acceptance criteria, finish, commit, and write REPORT.md.") \
       > "$out" 2> "$RUNS/attempt-$attempt.err"
  fi
  rc=$?
  json=$(grep -E '^\{' "$out" | tail -1)
  sid=$(printf '%s' "$json" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("session_id",""))
except Exception: print("")')
  [ -n "$sid" ] && SESSION=$sid
  iserr=$(printf '%s' "$json" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("is_error",True))
except Exception: print(True)')
  if [ $rc -eq 0 ] && [ "$iserr" = "False" ] && [ -f "$WT/REPORT.md" ]; then status=ok; break; fi
  echo "$(date -Is) attempt $attempt rc=$rc is_error=$iserr report=$([ -f "$WT/REPORT.md" ] && echo y || echo n)" >> "$RUNS/driver.log"
  sleep $(( attempt * 30 ))
done

echo "$status" > "$RUNS/status"
echo "$(date -Is) done status=$status attempts=$attempt branch=w/$NAME" >> "$RUNS/driver.log"
[ "$status" = ok ]
