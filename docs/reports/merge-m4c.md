# merge-m4c — report

## What was done
1. `git merge --no-ff w/m4c-automation`; M4c REPORT/PROGRESS moved to `docs/reports/m4c-automation.md` / `m4c-automation-progress.md`.
2. **TODO(M4a) resolved** (`automation/server_runner.py`, `runner.py`, `loops.py`, `scheduler.py`, `nlcron.py`, `gateway/server.py`, `config.py`):
   - `ServerRunner.run_prompt(kind=...)` sets `LiveSession.task_kind`; `_run_one_turn` uses it for tier policy, escalation and usage rows. Loop ticks → `loop_tick`, cron jobs → `cron_job`, automation prompt actions → `background_turn`; all cheap tier by default (`task_tiers` overrides).
   - `Runner.judge(system, user, kind)` now goes through `GatewayServer.oneshot(kind=...)` → `ModelCaller`: `--until` judge = `goal_judge`, NL→cron = `classification`. `automation.cheap_model` is deprecated (kept so configs load).
   - New `autonomy.gate_unattended` (default false): background (cron/loop/automation) sessions skip the plan-first gate (`PlanFirst.gate_applies`); they remain auto-mode pre-approved runs.
3. **`k3code slash`** deletes its throwaway session (`daemon.slash_via_daemon`, `session.delete`). Live check: session count unchanged (4 → 4) after 3 slash calls.
4. **Bug found and fixed**: `UsageDB.aggregate` never passed its bind args, so `/stats day` (default, 7-day filter) and any session filter raised "Incorrect number of bindings". Existing since M2; regression test added.

## Conflicts and resolutions
| File | Conflict | Resolution |
|---|---|---|
| `PROGRESS.md` | add/add | kept this branch's; M4c's moved to `docs/reports/m4c-automation-progress.md` |
| `REPORT.md` | M4c's | moved to `docs/reports/m4c-automation.md`; this file replaces it |
| `gateway/server.py` LiveSession fields | M4a tier/scope fields vs M4c `extra_tools`/`run_result`/`last_*` | kept both |
| `gateway/server.py` `GatewayServer.__init__` | M4a `_tiers`/`ModelCaller`/`PlanFirst` vs M4c `automation`/`last_user_activity` | kept both |
| `gateway/server.py` `_run_one_turn` / `_build_loop` | M4a split into `_build_loop` + tier/gate body; M4c reset `run_result`/`last_*` and installed `extra_tools`, added `_session_finished` | M4a structure; M4c resets at start of `_run_one_turn`; `extra_tools` installed in `_build_loop` (so escalated loops get `schedule_next`); `_session_finished` kept |
| semantic | M4c `ServerRunner` called `oneshot(model_key=cheap_model)` | now `kind=` via ModelCaller |
| `builtin.py`, `cli.py`, `config.py`, TUI files | auto-merged | checked; M1/M2/M4a/M4c registrations all present |

## Verification
- `cd core && timeout 900 uv run pytest -q -o addopts="" | tail -3` → `443 passed in 17s`; `uv run ruff check src tests` → All checks passed.
- `cd tui && npm ci && npm run build:ink && npm run build && npx vitest run` → build OK; `Test Files 1 failed | 174 passed`, `Tests 1 failed | 1589 passed | 2 skipped` (only the known `textInputFastEcho`).
- New tests: `test_unattended_kinds_use_cheap_tier_and_skip_gate`, `test_gate_unattended_option`, `test_stats_command_with_days_filter` (test_auto_gateway.py), `test_ticks_use_loop_tick_kind_and_until_judge_kind` (test_auto_loops.py).
- Live daemon acceptance (fake provider, temp home `/tmp/k3acc2`, `* * * * *` job; transcript in the commit's session `acceptance.txt`):

```
$ k3code schedule add "* * * * *" say hello --name hello
Scheduled b608dd0b “hello” (* * * * *).
$ k3code slash /stats
Usage per day:
- 2026-10-07: tokens 40/12 in/out, cost unknown, calls 4 [fake/m×4], failovers 0, ...
    tiers: cheap 4 calls (40/12 tok)
    kinds: cron_job×4
$ k3code slash "/stats session"
- 24244b9f…: calls 1 ... tiers: cheap 1 calls   kinds: cron_job×1   (×4 sessions)
$ k3code schedule list
b608dd0b  hello  [* * * * *]  active  runs: 4   #4 completed … api_calls=1 — cron says hello
```

## Deviations / notes
- The `test_unattended_kinds…` test asserts the tier requested from `tier_routers().get` (`cheap`) because the test server's fixture router is a single fallback chain that labels every row `main`; the live run shows the real `cheap` attribution.
- A cron job's explicit `model` is not used for routing: the tier policy decides (as for all `ModelCaller`/tier turns).
- Commit trailer follows the harness attribution (Sonnet 5.5) for the last commit where applicable.

## Open TODOs
- Webhook token stored in plain text in `automation.db`; `automation.update` event instead of polling; TUI view of automation run history (from M4c).
- Real-provider run of the live acceptance.
