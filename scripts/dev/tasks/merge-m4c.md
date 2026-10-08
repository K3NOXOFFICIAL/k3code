# merge-m4c: merge `w/m4c-automation` into this branch

This branch already contains M1-commands, M2-ops, M4a (tiers, routing and the scope gate) and the router cooldown fix. Read these first:
- `docs/reports/merge-m4a.md`
- `git show w/m4c-automation:REPORT.md`

## Do

1. **Merge.** Run `git merge --no-ff w/m4c-automation`.
   - Resolve the conflicts so that every feature works together.
   - Keep the loops, schedule, automations, webhook, suggestions and the TUI ⟳ badge from M4c, together with M4a's tiers, gate and ModelCaller, M1's goal and M2's multi-client daemon.
2. **Resolve M4c's `TODO(M4a)`.**
   - Loop ticks run with the `loop_tick` task kind.
   - Cron jobs run with the `cron_job` task kind.
   - Automation prompt actions run with the `background_turn` task kind.
   - All three go through the tier policy, which uses the cheap tier by default.
   - NL→cron conversion and the `--until` judge go through `ModelCaller` with the `classification` and `goal_judge` task kinds.
   - Unattended sessions (cron jobs, loop ticks and automations) skip the plan-first gate unless `autonomy.gate_unattended` is set. They are pre-approved unattended runs in `auto` mode.
3. **Fix `k3code slash`.** It currently leaves one empty session behind per call. Delete that session after the command runs.
4. **Reports.** Move REPORT/PROGRESS to `docs/reports/m4c-automation*.md`.

## Acceptance (put the outputs in REPORT.md)
- `cd core && timeout 900 uv run pytest -q -o addopts="" 2>&1 | tail -3` passes with more than 430 tests, and ruff is clean.
- `cd tui && npm ci && npm run build:ink && npm run build && npx vitest run`: only the known failure is allowed.
- Re-run the M4c live daemon acceptance (fake provider, temp home) and show `/stats` attributing the cron runs to the cheap tier with the `cron_job` kind.
- List each conflict and how it was resolved.
