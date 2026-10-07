# merge-m4b — report

Merged `w/m4b-fanout` (`git merge --no-ff`). Sub-agents, fan-out, /ultraplan, /ultracode, /ultraresearch, /bg and /artifacts now coexist with M1–M4a/M4c/M6. M4b's own report: `docs/reports/m4b-fanout.md` (progress: `m4b-fanout-progress.md`).

## Conflicts and resolutions
| File | Resolution |
|---|---|
| `PROGRESS.md` (deleted on our side, modified on theirs) | Removed from root; their copies moved to `docs/reports/m4b-fanout{,-progress}.md`. |
| `VENDOR.toml` | Union of both sides' entries (M4c loops.py + M4b design ports/atomic-agents). `vendor_check.py`: All checks passed. |
| `commands/builtin.py` (imports + registration list) | Kept both: M4c `AutomationsCommand/LoopCommand/ScheduleCommand` and M4b `ArtifactsCommand/BgCommand/UltraPlan/UltraCode/UltraResearch`. |
| `gateway/server.py` LiveSession fields | Kept M4c (`extra_tools`, `run_result`, `last_error`, `task_kind`, …) and M4b (`preapproved_plan`). |
| `gateway/server.py` server init | Kept `automation`/`last_user_activity` and `subagents/fanout/ultra/research`. |
| `gateway/server.py` loop build | Both: `session.extra_tools` installers (loop `schedule_next`) then `task`/web tools. |

## Integration changes
- `_active_rows` also lists running/queued sub-agent and fan-out children (`origin: "subagent"`, `background: true`, `parent_id`), next to `/bg` and M4c unattended sessions (those are live sessions with `background: true` and already shared the list). `SubagentManager._emit` broadcasts `session.active_list` on child start/complete. Test: `test_fanout.py::test_active_rows_include_running_children`.
- Setup wizard: `autonomy` now writes `{plan_first, fanout: {max_parallel}}` (was a dead `fanout_cap` key). `tests/test_setup.py` updated.
- `/artifacts publish` remains a stub.

## Verification
- `cd core && timeout 900 uv run pytest -q -o addopts=""` → `515 passed in 30.29s`. `uv run ruff check src tests scripts` clean (one import-order autofix). `python3 scripts/vendor_check.py` → All checks passed.
- `cd tui && npm ci && npm run build:ink && npm run build && npx vitest run` → builds OK; `2 failed | 1588 passed`. Failures: the known `textInputFastEcho` and `virtualHistoryOffsetCache` "corrects and compensates a same-layout row" — the latter is a load flake (passes 17/17 when run alone, twice; the merge touched no related code).
- `uv run python scripts/demo_ultracode.py` → passes: `fanout.done merged 3/3, tests pass`, `Budget used: 12/12 agents, 450/2000000 tokens`.
- `k3code slash /help` (against a temp-`K3CODE_HOME` daemon) lists: /add-dir /advisor /artifacts /automations /bg /branch /clear /compact /config /daemon /debug /doctor /effort /exit /export /fork /go /goal /help /import /loop /mcp /memory /model /output-style /preview /proposals /rename /resume /review /schedule /scope /settings /skills /stats /stop /ultracode /ultraplan /ultraresearch /update.
  Not in `/help`: `/setup` (it is the CLI subcommand `k3code setup`, not a slash command). I did not check GOAL.md's command list line by line beyond that.

## Open TODOs
- Live `/ultraresearch` still unrun (quota / SearXNG unreachable earlier).
- Child rows use a monotonic `started_at`, so their `last_active`/`started_at` aren't wall-clock; only status/title are meaningful in the strip.
- `/artifacts publish` stub; M5 not merged.
