# merge-m4a: merge `w/m4a-autonomy` into this branch and fix the long Retry-After bug

## Setup

This branch contains M2-ops, M1-commands and the merge between them. `w/m4a-autonomy` was developed in parallel from an older base. It adds:
- tiers and routing,
- the scope gate and plan-first,
- proposals,
- preview,
- advisor,
- TUI proposal cards.

Read `git show w/m4a-autonomy:REPORT.md` and `docs/reports/merge-m1-commands.md`.

## Do

1. **Merge.** Run `git merge --no-ff w/m4a-autonomy`.
   - Resolve conflicts so that **all** features work together.
   - In particular, `gateway/server.py` `_run_turn` must combine M1's goal loop, M2's multi-client session routing and reliability, and M4a's scope gate, tier loops, escalation and plan confirmation.
   - Wire M4a's open TODO now that `/goal` exists: the goal's done-check calls `advisor.review_done` when `autonomy.advisor_on_goal` is enabled, and the goal judge uses the `goal_judge` task kind through `ModelCaller`.
   - Route titles and compaction through `ModelCaller` too (`title` and `compaction` task kinds).
   - Move REPORT/PROGRESS files to `docs/reports/m4a-autonomy*.md`.
2. **Bug fix: a long Retry-After should fail over, not sleep.**
   - Today, a 429 with `retry_after ≈ 59826 s` (provider daily quota) makes the router sleep on the first chain entry.
   - **New rule:** if `retry_after` is longer than `router.max_inline_wait` (default 20 s), put that entry, or that entry's model, in cooldown until the reset time and **fail over immediately**. Do the same for any quota-type error even without a Retry-After (default cooldown 1 h).
   - Only when **all** entries are cooling down does `persistent_retry` park, until the earliest reset, and emit `reliability.parked` with the time. Make `/preview` and other budgeted calls fail fast with a clear "all providers rate-limited until HH:MM" message.
   - Persist cooldowns in `$K3CODE_HOME/cooldowns.json` so that a daemon restart doesn't hammer a quota-exhausted provider.
   - Add tests for each case.
3. **Pytest pipe hang.** The M4a report says piping pytest output hangs because the suite leaves child processes holding the pipe. Find the leaking fixture (likely daemon, MCP stdio or bash tests) and make sure every spawned process is killed at teardown, so that `uv run pytest -q -o addopts="" | tail -3` returns.

## Acceptance (put the outputs in REPORT.md)
- `cd core && timeout 900 uv run pytest -q -o addopts="" 2>&1 | tail -3` passes, with more than 340 tests, and returns promptly. ruff is clean.
- `cd tui && npm ci && npm run build:ink && npm run build && npx vitest run`: only the known `textInputFastEcho` failure is allowed.
- List the conflicts and their resolutions.
