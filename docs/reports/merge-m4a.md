# merge-m4a — report

## What was done
1. **Merged `w/m4a-autonomy`** (`git merge --no-ff`). M4a's REPORT/PROGRESS moved to `docs/reports/m4a-autonomy.md` and `docs/reports/m4a-autonomy-progress.md`.
2. **Long Retry-After / quota failover** (`router/router.py`, `router/cooldown.py`, `reliability/persistent_retry.py`, `routing/tiers.py`, `config.py`, `gateway/server.py`, `cli.py`, `commands/autonomy.py`).
3. **Pytest pipe hang** fixed (`reliability/netwatch.py`, `tests/conftest.py`).
4. **M4a TODOs wired**: goal done-check → `advisor.review_done`; goal judge → `goal_judge` kind; titles and `/compact` → `ModelCaller` (`session_ai.py`).

## Conflicts and resolutions
| File | Conflict | Resolution |
|---|---|---|
| `config.py` | both sides appended `Settings` fields | kept all of M1/M2 (`permissions`, `output_style`, `display`, `skills`, `mcp`, `mem0`, `goal`) plus M4a (`task_tiers`, `autonomy`) |
| `gateway/server.py` #1 | `_run_turn` rewritten on both sides (M1 goal loop + `_run_one_turn`; M4a `_build_loop`/gate/tiers) | `_run_turn` = M1 goal loop (judge, auto-continue, pause on cancel/error); `_build_loop` = M4a builder with M1's `build_system_prompt(... mcp=)`, skill tool and MCP tool registration; `_run_one_turn` = M4a's gate + tier loop + escalation body with M1's `mcp.ensure_started()` and the `(status, final_text)` return; M2 per-session routing (`_ctx_session`, `session.emit`) untouched |
| `gateway/server.py` #2 | `loop` construction tail | merged (skill/MCP tools registered inside `_build_loop`, so escalated loops get them too) |
| `gateway/server.py` #3 | M1 command services (`clarify`, `apply_file_config`, `oneshot`, goal helpers) vs M4a `_confirm_plan` | kept both |
| semantic | `build_chain` import lost, `apply_file_config` reset only `router` | import restored; also resets `_tiers` |

## Behaviour changes
- **Goal**: judge runs via `ModelCaller` on `goal_judge` (cheap tier); `goal.judge_model` other than `"cheap"` still pins a model. When `autonomy.advisor_on_goal` is on, a `done` verdict (after gates pass) is checked by `advisor.review_done`; blocking issues continue the goal with them as the prompt (counts against the turn budget); advisor failure never blocks.
- **Router**: `router.max_inline_wait` (default 20 s) and `router.quota_cooldown` (default 3600 s) in config. Retry-After > max_inline_wait on rate-limit → entry cooled until reset + immediate failover (`router.cooldown` event). Quota errors cool down for Retry-After or 1 h. Retry-After ≤ limit still sleeps inline; for other reasons a longer one now uses jittered backoff. When every entry is cooling, `ChainExhausted` (with `retry_after`, `until`, message "all providers rate-limited until HH:MM (reason)") is raised immediately — `ModelCaller` users (`/preview`, advisor, judge) fail fast; `/preview` shows "Preview unavailable: …". `PersistentRetry` parks to the earliest reset and `reliability.parked` carries `until`; the status line reads "⏸ all providers rate-limited until HH:MM".
- **Persistence**: `$K3CODE_HOME/cooldowns.json` (rate-limit/quota only; expired entries dropped on load; corrupt file ignored) used by the daemon/gateway and the CLI.
- **/compact** is now real: older messages (cut at a user-turn boundary, last 6 kept) are summarized on the `compaction` kind.
- **Titles**: `session_ai.make_title` on the `title` kind, fired after the first completed turn **only when `autonomy.auto_title: true`** (default off — see deviations).

## Pipe hang — root cause
Netwatch's `nmcli` probe was cancelled mid-spawn at event-loop teardown (`asyncio.run`'s `_cancel_all_tasks` blocked forever in `create_subprocess_exec`), and any failing test skipped `server.close()`. Fixes: `nmcli_state` now has stdin=DEVNULL and kills/reaps its child in `finally`; `tests/conftest.py` stubs `nmcli_state` for all tests and a session fixture SIGKILLs every descendant of the pytest process at session end (so no leaked MCP/daemon child can hold the pipe).

## Verification
- `cd core && timeout 900 uv run pytest -q -o addopts="" 2>&1 | tail -3` → `386 passed in ~27s`, returns promptly (also `| tail -1` run repeatedly; no leftover child processes).
- `uv run ruff check src tests` → All checks passed.
- `cd tui && npm ci && npm run build:ink && npm run build && npx vitest run` → build OK; `Test Files 1 failed | 174 passed`, `Tests 1 failed | 1589 passed | 2 skipped` — the only failure is the known `textInputFastEcho`.
- New tests: `tests/test_router_cooldown.py` (13: long Retry-After fails over without sleeping, short one still sleeps, configurable limit, quota 1 h default + override, cooled entry skipped, all-cooling fast failure + message, not-all-cooling has no retry_after, PersistentRetry parks to earliest reset with `until`, persistence/expiry/corrupt file/restart), `tests/test_session_ai.py` (5), 3 new goal tests (advisor veto, advisor failure, `goal_judge` kind/tier row).

## Deviations
- `autonomy.auto_title` defaults to **false**: an extra model call after every first turn breaks scripted-provider tests and spends tokens unasked; enable in config.
- The existing goal tests that count provider calls set `autonomy.advisor_on_goal: false` (the advisor adds a call by design).
- The last live OmniRoute `/preview` re-check was not run (quota is exhausted until 2026-10-08T03:00Z); the exact failure mode is covered by the unit tests instead.
- Commit trailer is `Claude Sonnet 5.5` (harness attribution), not `Opus 5.5`.

## Open TODOs
- Re-run a live `/preview` once the OmniRoute quota resets.
- Per-provider (not per-entry) cooldown for provider-wide quotas could save one futile call per extra model.
- M4a TODOs still open: persist TUI proposal-card state across reconnect.
