#!/usr/bin/env bash
# omni-worker.sh — run one k3code build task with a headless Claude Code worker
# whose model calls go through OmniRoute (not the Claude account).
#
# usage: scripts/dev/omni-worker.sh <task.md> <name> [base-branch] [model]
#   task.md      task spec (self-contained; see scripts/dev/tasks/)
#   name         worker name -> branch w/<name>, worktree .claude/worktrees/w-<name>
#   base-branch  branch to fork from (default: current branch of the repo root checkout)
#   models       comma-separated OmniRoute combos, tried in order (default: auto/muse,auto/pro-coding,auto/coding-manual).
#                The driver rotates to the next combo after K3DEV_ROTATE_AFTER consecutive stalls
#                (worker stopped without REPORT.md); the next combo is also passed as --fallback-model.
#
# Retries until the worker reports success (bounded by K3DEV_MAX_ATTEMPTS),
# resuming the same session after API/network failures. Logs: .k3dev/runs/<name>/
set -uo pipefail

TASK=${1:?task file}; NAME=${2:?name}; BASE=${3:-}; MODELS=${4:-auto/muse,auto/pro-coding,auto/coding-manual}
SETTINGS=${K3DEV_SETTINGS:-$HOME/.claude/settings.omniroute.json}
MAX_ATTEMPTS=${K3DEV_MAX_ATTEMPTS:-30}
ROTATE_AFTER=${K3DEV_ROTATE_AFTER:-2}
IFS=, read -r -a MODEL_LIST <<< "$MODELS"
midx=0; stalls=0
REPO=$(git -C "$(dirname "$0")" rev-parse --path-format=absolute --git-common-dir | sed 's#/\.git$##')
WT="$REPO/.claude/worktrees/w-$NAME"
RUNS="$REPO/.k3dev/runs/$NAME"
PREAMBLE="$(dirname "$(readlink -f "$0")")/worker-preamble.md"
mkdir -p "$RUNS"
TASK=$(readlink -f "$TASK")

[ -f "$SETTINGS" ] || { echo "missing $SETTINGS" >&2; exit 2; }
# Same OmniRoute key, exposed to the worker's tools for live smoke tests (never printed).
if [ -z "${OMNIROUTE_API_KEY:-}" ]; then
  OMNIROUTE_API_KEY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["env"].get("ANTHROPIC_AUTH_TOKEN",""))' "$SETTINGS")
  export OMNIROUTE_API_KEY
fi

RESUMING=0
if [ ! -d "$WT" ]; then
  BASE=${BASE:-$(git -C "$REPO" rev-parse --abbrev-ref HEAD)}
  git -C "$REPO" worktree add -q "$WT" -b "w/$NAME" "$BASE" || exit 2
else
  RESUMING=1   # restarted driver on an existing worktree: continue from its state
fi

wait_online() {  # pause while offline / gateway unreachable, resume automatically
  local n=0
  until curl -s -o /dev/null -m 10 https://<omniroute-public-host>/; do
    [ $((n % 6)) -eq 0 ] && echo "$(date -Is) offline/gateway down — waiting" >> "$RUNS/driver.log"
    n=$((n + 1)); sleep 20
  done
}

base_prompt() { printf '%s\n\n## Your task\n%s\n' "$(cat "$PREAMBLE")" "$(cat "$TASK")"; }
resume_prompt() {
  base_prompt
  printf '\n## Resuming\n%s\n' "An earlier worker session already worked on this task in this same worktree and did not finish. First read PROGRESS.md (if present), then run git log --oneline -20, git status and git diff --stat to see what is already done. Do NOT redo finished work; continue with what is missing, then verify, commit and write REPORT.md."
}
if [ "$RESUMING" = 1 ]; then PROMPT=$(resume_prompt); else PROMPT=$(base_prompt); fi

SESSION=""; attempt=0; status=fail
while [ $attempt -lt "$MAX_ATTEMPTS" ]; do
  attempt=$((attempt + 1)); wait_online
  MODEL=${MODEL_LIST[$midx]}
  FALLBACK=${MODEL_LIST[$(( (midx + 1) % ${#MODEL_LIST[@]} ))]}
  FB=(); [ "$FALLBACK" != "$MODEL" ] && FB=(--fallback-model "$FALLBACK")
  out="$RUNS/attempt-$attempt.json"; stamp="$RUNS/.attempt-start"; touch "$stamp"
  echo "$(date -Is) attempt $attempt model=$MODEL (session=${SESSION:-new})" >> "$RUNS/driver.log"
  if [ -z "$SESSION" ]; then
    (cd "$WT" && nice -n 10 ionice -c3 claude -p --settings "$SETTINGS" --model "$MODEL" "${FB[@]}" \
       --permission-mode auto --output-format json "$PROMPT") > "$out" 2> "$RUNS/attempt-$attempt.err"
  else
    (cd "$WT" && nice -n 10 ionice -c3 claude -p --settings "$SETTINGS" --model "$MODEL" "${FB[@]}" \
       --permission-mode auto --output-format json --resume "$SESSION" \
       "You stopped before finishing. Do NOT stop to announce next steps — keep calling tools until the whole task is done. Continue exactly where you left off, then verify the acceptance criteria, commit, and write REPORT.md.") \
       > "$out" 2> "$RUNS/attempt-$attempt.err"
  fi
  rc=$?
  json=$(grep -E '^\{' "$out" | tail -1)
  sid=$(printf '%s' "$json" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("session_id",""))
except Exception: print("")')
  [ -n "$sid" ] && SESSION=$sid
  # Context exhausted (or compaction failed): continue in a fresh session from the worktree state.
  if grep -qiE "prompt is too long|compaction failed|context.{0,20}(length|window)" "$out" "$RUNS/attempt-$attempt.err" 2>/dev/null; then
    echo "$(date -Is) context exhausted — next attempt starts a fresh session" >> "$RUNS/driver.log"
    SESSION=""
    PROMPT=$(resume_prompt)
  fi
  iserr=$(printf '%s' "$json" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("is_error",True))
except Exception: print(True)')
  if [ $rc -eq 0 ] && [ "$iserr" = "False" ] && [ "$WT/REPORT.md" -nt "$stamp" ]; then status=ok; break; fi
  echo "$(date -Is) attempt $attempt rc=$rc is_error=$iserr report=$([ -f "$WT/REPORT.md" ] && echo y || echo n)" >> "$RUNS/driver.log"
  stalls=$((stalls + 1))
  if [ "$stalls" -ge "$ROTATE_AFTER" ] && [ "${#MODEL_LIST[@]}" -gt 1 ]; then
    midx=$(( (midx + 1) % ${#MODEL_LIST[@]} )); stalls=0
    echo "$(date -Is) rotating to ${MODEL_LIST[$midx]}" >> "$RUNS/driver.log"
  fi
  sleep $(( attempt < 6 ? attempt * 20 : 120 ))
done

echo "$status" > "$RUNS/status"
echo "$(date -Is) done status=$status attempts=$attempt branch=w/$NAME" >> "$RUNS/driver.log"
[ "$status" = ok ]
