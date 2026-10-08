# merge-m4b: merge `w/m4b-fanout` into this branch

This branch contains everything except M4b and M5: M1, M2, M3 keys, M4a, M4c and M6 (merged). Read these first:
- `git show w/m4b-fanout:REPORT.md`
- `docs/reports/merge-m4c.md`
- `docs/reports/m6-install.md`

## Do

1. **Merge.** Run `git merge --no-ff w/m4b-fanout`. Expect conflicts in `core/src/k3code/gateway/server.py`, `core/src/k3code/commands/builtin.py`, `VENDOR.toml` and `PROGRESS.md`.
   - Resolve them so that everything works together. M4b's sub-agents, fan-out, ultra commands, `/bg` and artifacts must coexist with:
     - M4c's loops, schedule and automations (`ServerRunner`, unattended task kinds);
     - M4a's gate and tiers;
     - M1's goal;
     - M2's multi-client daemon;
     - M6's setup and update commands.
   - Keep every command registered; check that `/help` lists all of them.
   - `VENDOR.toml` is the union of both sides' entries.
2. **Integration points.**
   - Background sessions from `/bg` and fan-out children appear in the same strip and `active_list` as M4c's unattended sessions.
   - The M6 setup wizard's `autonomy` keys include `fanout.max_parallel`.
   - `/artifacts publish` stays a stub.
3. **Reports.** Move REPORT/PROGRESS to `docs/reports/m4b-fanout*.md`.

## Acceptance (put the outputs in REPORT.md)
- `cd core && timeout 900 uv run pytest -q -o addopts="" 2>&1 | tail -3` passes with more than 510 tests. ruff and vendor_check are clean.
- `cd tui && npm ci && npm run build:ink && npm run build && npx vitest run`: only the known failure is allowed.
- `uv run python scripts/demo_ultracode.py` still passes after the merge.
- `k3code slash /help` lists every command from GOAL.md that exists so far. Paste the list, and note any missing commands.
- List each conflict and how it was resolved.
