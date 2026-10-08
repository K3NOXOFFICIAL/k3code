# merge-m5 report

## What was done
- `git merge --no-ff w/m5-learning`; conflicts in `VENDOR.toml` (kept both), `commands/builtin.py` (registered M4b/M4c commands + M5 ones) and `gateway/server.py` (`/stop` now records the interrupt decision *and* interrupts sub-agents).
- **Actor tagging**: `DecisionLog.record(actor=...)` stores `detail.actor`; `LearningHub.record` tags `auto` for background/unattended sessions (bg, loop ticks, cron, automations) and `user` otherwise. `DecisionLog.query` defaults to `actor="user"`, so every miner (permrules, distiller, ranking, optimizer) sees only user decisions (`actor=None` = all). Sub-agent and fan-out approvals go through the parent session's approval callback, so they are recorded under the parent session (user in interactive parents, auto in unattended ones).
- **/permissions** (`commands/permissions_cmd.py`): overview (mode, rules with ids and source builtin/user/project/session, hardline list), `mode`, `allow|ask|deny <tool> <pattern> [--project|--user|--session]`, `rm <id>` (ids like `u1`, `p2`, `s1`; builtin rules are not removable), `suggest` (M5).
- **/focus** registered on the gateway (`FocusCommand`); `config.set` accepts `display.focus` (and the TUI's legacy `focus`) → `display.focus_mode`.
- **Test** `core/tests/test_merge_m5.py`: parses GOAL.md's command list, asserts each is registered and appears in `/help`; /permissions show/add/rm, /focus, actor tagging.
- M5 reports moved to `docs/reports/m5-learning*.md`.

## Verification
- `cd core && timeout 900 uv run pytest -q -o addopts="" | tail -3` → **599 passed**.
- `uv run ruff check .` → All checks passed; `python3 scripts/vendor_check.py` → All checks passed.
- TUI: `npm ci && npm run build:ink && npm run build && npx vitest run` → 1590 passed, 2 skipped, **1 failed**: `textInputFastEcho.test.ts > colorizeEcho > passes through on a non-color value` (vendored Hermes TUI file, untouched by this merge; the merge changes only 3 TUI files, all proposal icons).
- `uv run python ../scripts/demo_m5.py` → OK; `uv run python scripts/demo_ultracode.py` (run from core/) → exit 0.
- `k3code slash /help` (temp K3CODE_HOME daemon): lists every GOAL command plus /focus; full output in the test above and below.

## /help (excerpt, all GOAL commands present)
/add-dir /advisor /artifacts /automations /bg /branch /clear /compact /config /daemon /debug /doctor /effort /exit /export /focus /fork /go /goal /help /import /learn /loop /loop /mcp /memory /model /optimizer /output-style /permissions /preview /proposals /rename /resume /review /schedule /schedule /scope /self-improve /settings /skills /stats /stop /ultracode /ultraplan /ultraresearch /update /update-config 

## Deviations / open TODOs
- `DecisionLog.query(limit=…)` applies LIMIT before the actor filter (no caller passes a limit).
- Sub-agent decisions are not tagged with the child id; they share the parent's session id.
- /self-improve is still a stub; no live-model run of distiller/review/update-config.
- demo_ultracode.py lives in `core/scripts/`, demo_m5.py in `scripts/`.
