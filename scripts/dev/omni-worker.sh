#!/usr/bin/env bash
# omni-worker.sh — run one k3code build task with a headless Claude Code worker.
# Default (since 2026-10-07): native Claude Code, Sonnet 5.5, no OmniRoute traffic at all.
# With K3DEV_ALLOW_OMNIROUTE=1 the model calls go through OmniRoute first (saves Claude tokens)
# and Claude is only the last resort.
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

# Run from a private snapshot so edits to this file never disturb running drivers
# (bash reads scripts incrementally while executing them).
if [ -z "${K3DEV_SNAPSHOT:-}" ]; then
  snap=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/omni-worker.XXXXXX.sh")
  cp "$0" "$snap"
  K3DEV_SNAPSHOT=$snap K3DEV_SELF_DIR=$(dirname "$(readlink -f "$0")") exec bash "$snap" "$@"
fi
SELF_DIR=${K3DEV_SELF_DIR:-$(dirname "$(readlink -f "$0")")}

TASK=${1:?task file}; NAME=${2:?name}; BASE=${3:-}; MODELS=${4:-auto/muse,auto/pro-coding,auto/coding-manual}
SETTINGS=${K3DEV_SETTINGS:-$HOME/.claude/settings.omniroute.json}
# OmniRoute is OFF by default (owner decision 2026-10-07: the key's daily usage limit was used up).
# Workers then run on native Claude Code (Sonnet 5.5) and never read or send the OmniRoute key.
# Opt back in with K3DEV_ALLOW_OMNIROUTE=1.
ALLOW_OMNI=${K3DEV_ALLOW_OMNIROUTE:-0}
MAX_ATTEMPTS=${K3DEV_MAX_ATTEMPTS:-30}
ROTATE_AFTER=${K3DEV_ROTATE_AFTER:-2}
IFS=, read -r -a MODEL_LIST <<< "$MODELS"
midx=0; stalls=0
REPO=$(git -C "$SELF_DIR" rev-parse --path-format=absolute --git-common-dir | sed 's#/\.git$##')
WT="$REPO/.claude/worktrees/w-$NAME"
RUNS="$REPO/.k3dev/runs/$NAME"
PREAMBLE="$SELF_DIR/worker-preamble.md"
mkdir -p "$RUNS"
TASK=$(readlink -f "$TASK")

if [ "$ALLOW_OMNI" = 1 ]; then
  [ -f "$SETTINGS" ] || { echo "missing $SETTINGS" >&2; exit 2; }
  # Same OmniRoute key, exposed to the worker's tools for live smoke tests (never printed).
  if [ -z "${OMNIROUTE_API_KEY:-}" ]; then
    OMNIROUTE_API_KEY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["env"].get("ANTHROPIC_AUTH_TOKEN",""))' "$SETTINGS")
    export OMNIROUTE_API_KEY
  fi
else
  # Make sure nothing the worker runs can pick up an OmniRoute key from this shell.
  unset OMNIROUTE_API_KEY K3_ALLOW_OMNIROUTE
  export K3_ALLOW_OMNIROUTE=0
fi

RESUMING=0
if [ ! -d "$WT" ]; then
  BASE=${BASE:-$(git -C "$REPO" rev-parse --abbrev-ref HEAD)}
  git -C "$REPO" worktree add -q "$WT" -b "w/$NAME" "$BASE" || exit 2
else
  RESUMING=1   # restarted driver on an existing worktree: continue from its state
fi

# Prefer OmniRoute's tailnet address: the public URL sits behind Cloudflare, which cuts
# responses after ~100 s (HTTP 524). The settings copy lives in RAM (XDG_RUNTIME_DIR, 0600)
# and is removed on exit; it holds the same key as the source profile.
DIRECT_URL=${K3DEV_DIRECT_URL:-http://<omniroute-host>:20128}
RUNTIME_SETTINGS=""
if [ "$ALLOW_OMNI" != 1 ]; then
  HEALTH_URL="https://api.anthropic.com/"   # Claude only: wait for Anthropic, never contact OmniRoute
elif curl -s -o /dev/null -m 5 "$DIRECT_URL/"; then
  RUNTIME_SETTINGS=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/k3dev-settings.XXXXXX.json")
  chmod 600 "$RUNTIME_SETTINGS"
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d.setdefault("env",{})["ANTHROPIC_BASE_URL"]=sys.argv[2]; d["env"]["API_TIMEOUT_MS"]="900000"; d["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"]="1"; d["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]="110000"; d["env"]["CLAUDE_CODE_DISABLE_THINKING"]="1"; d["env"]["DISABLE_PROMPT_CACHING"]="1"; json.dump(d,open(sys.argv[3],"w"))' \
    "$SETTINGS" "$DIRECT_URL" "$RUNTIME_SETTINGS"
  trap 'rm -f "$RUNTIME_SETTINGS"' EXIT
  trap 'exit 143' INT TERM HUP
  SETTINGS=$RUNTIME_SETTINGS
  HEALTH_URL="$DIRECT_URL/"
else
  HEALTH_URL="https://<omniroute-public-host>/"
fi
echo "$(date -Is) gateway=$HEALTH_URL" >> "$RUNS/driver.log"

# Last resort: native Claude Code (Claude account) when OmniRoute cannot serve the work —
# better to keep working on Claude than to stop entirely.
CLAUDE_FALLBACK_MODEL=${K3DEV_CLAUDE_FALLBACK:-claude-sonnet-5-5}
GATEWAY_DOWN_GRACE=${K3DEV_GATEWAY_DOWN_GRACE:-600}   # seconds of OmniRoute outage before using Claude
USE_CLAUDE=0
# Every worker (and everything it spawns: tests, builds, daemons) runs in its own cgroup scope, so a
# runaway process tree cannot take the laptop down (a recursing shell loop did exactly that once).
SCOPE=()
if [ "${K3DEV_NO_SCOPE:-0}" != 1 ] && command -v systemd-run >/dev/null 2>&1; then
  SCOPE=(systemd-run --user --scope -q -p "MemoryMax=${K3DEV_MEM_MAX:-6G}" -p "TasksMax=${K3DEV_TASKS_MAX:-1500}" --)
fi
IDLE_KILL=${K3DEV_IDLE_KILL:-480}   # seconds without transcript activity before a call counts as hung
PROJ_DIR="$HOME/.claude/projects/$(printf '%s' "$WT" | sed 's#[/.]#-#g')"

# Lean workers: no MCP servers and no skills (they cost ~30K tokens of context per call and
# workers don't need them); auto-memory off and earlier compaction for the Claude path too.
LEAN=(--strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands)
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1

wait_online() {  # pause while offline; if only OmniRoute is down for long, switch to Claude
  local n=0 start
  start=$(date +%s)
  until curl -s -o /dev/null -m 10 "$HEALTH_URL"; do
    if curl -s -o /dev/null -m 10 https://api.anthropic.com/ \
       && [ $(( $(date +%s) - start )) -ge "$GATEWAY_DOWN_GRACE" ]; then
      echo "$(date -Is) OmniRoute down >${GATEWAY_DOWN_GRACE}s but internet up — Claude fallback" >> "$RUNS/driver.log"
      USE_CLAUDE=1; return
    fi
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

SESSION=""; attempt=0; status=fail; omni_errors=0; LAST_BACKEND=""; KEY_QUOTA_UNTIL=0
run_stamp="$RUNS/.run-start"; touch "$run_stamp"   # a REPORT.md newer than this counts as done
while [ $attempt -lt "$MAX_ATTEMPTS" ]; do
  attempt=$((attempt + 1))
  # While the OmniRoute key itself is out of quota, stay on Claude until the reset.
  [ "$(date +%s)" -lt "$KEY_QUOTA_UNTIL" ] && USE_CLAUDE=1
  [ "$ALLOW_OMNI" = 1 ] || USE_CLAUDE=1
  # OmniRoute mode waits for the gateway; Claude-only mode waits for api.anthropic.com (offline pause).
  if [ "$USE_CLAUDE" != 1 ] || [ "$ALLOW_OMNI" != 1 ]; then wait_online; fi
  if [ "$USE_CLAUDE" = 1 ]; then
    MODEL=$CLAUDE_FALLBACK_MODEL; FB=(); SET=("${LEAN[@]}")   # default settings = native Claude account
    # Never resume a session written by other models on Claude: foreign thinking blocks fail
    # signature checks (HTTP 400). Start fresh from the worktree state; resume only Claude's own.
    if [ "$LAST_BACKEND" != claude ]; then SESSION=""; PROMPT=$(resume_prompt); fi
    LAST_BACKEND=claude
  else
    if [ "$LAST_BACKEND" = claude ]; then SESSION=""; PROMPT=$(resume_prompt); fi
    LAST_BACKEND=omniroute
    MODEL=${MODEL_LIST[$midx]}
    FALLBACK=${MODEL_LIST[$(( (midx + 1) % ${#MODEL_LIST[@]} ))]}
    FB=(); [ "$FALLBACK" != "$MODEL" ] && FB=(--fallback-model "$FALLBACK")
    SET=(--settings "$SETTINGS" "${LEAN[@]}")
  fi
  out="$RUNS/attempt-$attempt.json"; stamp="$RUNS/.attempt-start"; touch "$stamp"
  echo "$(date -Is) attempt $attempt model=$MODEL$([ "$USE_CLAUDE" = 1 ] && echo ' [CLAUDE FALLBACK]') (session=${SESSION:-new})" >> "$RUNS/driver.log"
  if [ -z "$SESSION" ]; then
    MSG=$PROMPT; RES=()
  else
    MSG="You stopped before finishing. Do NOT stop to announce next steps — keep calling tools until the whole task is done. Continue exactly where you left off, then verify the acceptance criteria, commit, and write REPORT.md."
    RES=(--resume "$SESSION")
  fi
  (cd "$WT" && exec "${SCOPE[@]}" nice -n 10 ionice -c3 claude -p "${SET[@]}" --model "$MODEL" "${FB[@]}" \
     --permission-mode auto --output-format json "${RES[@]}" "$MSG") > "$out" 2> "$RUNS/attempt-$attempt.err" &
  cpid=$!
  # Idle watchdog: a hung model call leaves the session transcript untouched; kill and resume.
  killed_idle=0
  while kill -0 "$cpid" 2>/dev/null; do
    sleep 30
    newest=$(find "$PROJ_DIR" -maxdepth 1 -name '*.jsonl' -newer "$stamp" -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1)
    last=${newest%% *}; last=${last%.*}; [ -z "$last" ] && last=$(stat -c %Y "$stamp")
    if [ $(( $(date +%s) - last )) -ge "$IDLE_KILL" ]; then
      echo "$(date -Is) worker idle >${IDLE_KILL}s (hung call) — killing and resuming" >> "$RUNS/driver.log"
      kill "$cpid" 2>/dev/null; sleep 5; kill -9 "$cpid" 2>/dev/null
      killed_idle=1
      [ -n "$newest" ] && SESSION=$(basename "${newest#* }" .jsonl)
      break
    fi
  done
  wait "$cpid" 2>/dev/null
  rc=$?; [ "$killed_idle" = 1 ] && rc=124
  json=$(grep -E '^\{' "$out" | tail -1)
  sid=$(printf '%s' "$json" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("session_id",""))
except Exception: print("")')
  [ -n "$sid" ] && SESSION=$sid
  # Context exhausted (or compaction failed): continue in a fresh session from the worktree state.
  if printf '%s' "$json" | python3 -c 'import sys,json,re
try: r=json.load(sys.stdin).get("result") or ""
except Exception: r=""
sys.exit(0 if re.search(r"prompt is too long|compaction failed|context length exceeded|maximum context|invalid .?signature.? in .?thinking|blocks with cache_control", r, re.I) else 1)'; then
    echo "$(date -Is) context exhausted or session incompatible with model — next attempt starts a fresh session" >> "$RUNS/driver.log"
    SESSION=""
    PROMPT=$(resume_prompt)
  fi
  iserr=$(printf '%s' "$json" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("is_error",True))
except Exception: print(True)')
  if [ $rc -eq 0 ] && [ "$iserr" = "False" ] && [ "$WT/REPORT.md" -nt "$run_stamp" ]; then status=ok; break; fi
  # Finished but the process hung on exit (e.g. a leftover child held stdout): a fresh report counts.
  if [ "$killed_idle" = 1 ] && [ "$WT/REPORT.md" -nt "$run_stamp" ] \
     && git -C "$WT" diff --quiet HEAD 2>/dev/null; then status=ok; break; fi
  echo "$(date -Is) attempt $attempt rc=$rc is_error=$iserr report=$([ -f "$WT/REPORT.md" ] && echo y || echo n)" >> "$RUNS/driver.log"
  # The OmniRoute key's own daily quota is exhausted: no combo can work until the reset.
  if [ "$iserr" != "False" ] && printf '%s' "$json" | grep -qiE 'API key reached its daily usage quota'; then
    h=$(printf '%s' "$json" | grep -oE 'Resets in [0-9]+h' | grep -oE '[0-9]+' | head -1)
    KEY_QUOTA_UNTIL=$(( $(date +%s) + ${h:-1} * 3600 ))
    echo "$(date -Is) OmniRoute key quota exhausted — Claude fallback for ${h:-1}h" >> "$RUNS/driver.log"
  fi
  if [ "$USE_CLAUDE" = 1 ]; then
    # One Claude attempt done; go back to OmniRoute next time (unless the key quota is still out).
    USE_CLAUDE=0; omni_errors=0
    sleep 30; continue
  fi
  stalls=$((stalls + 1))
  # API error / quota / rate-limit on this combo (e.g. "429 … reset after 20h"): rotate right away.
  if [ "$iserr" != "False" ] || printf '%s' "$json" | grep -qiE '\(429\)|rate.?limit|quota|reset after|all targets were skipped'; then
    stalls=$ROTATE_AFTER; omni_errors=$((omni_errors + 1))
    echo "$(date -Is) $MODEL errored (${omni_errors} in a row) — rotating now" >> "$RUNS/driver.log"
  else
    omni_errors=0
  fi
  # Every OmniRoute combo errored in a row: last resort is native Claude Code.
  if [ "$omni_errors" -ge "${#MODEL_LIST[@]}" ]; then
    USE_CLAUDE=1
    echo "$(date -Is) all OmniRoute combos failing — next attempt on Claude ($CLAUDE_FALLBACK_MODEL)" >> "$RUNS/driver.log"
  fi
  if [ "$stalls" -ge "$ROTATE_AFTER" ] && [ "${#MODEL_LIST[@]}" -gt 1 ]; then
    midx=$(( (midx + 1) % ${#MODEL_LIST[@]} )); stalls=0
    echo "$(date -Is) rotating to ${MODEL_LIST[$midx]}" >> "$RUNS/driver.log"
  fi
  sleep $(( attempt < 6 ? attempt * 20 : 120 ))
done

echo "$status" > "$RUNS/status"
echo "$(date -Is) done status=$status attempts=$attempt branch=w/$NAME" >> "$RUNS/driver.log"
[ "$status" = ok ]
