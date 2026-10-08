# merge-m5: merge `w/m5-learning` and complete the command set

This branch contains every milestone except M5. Read `git show w/m5-learning:REPORT.md`, `docs/reports/merge-m4b.md` and `docs/reports/m3-panes.md`.

## Do

1. **Merge.** Run `git merge --no-ff w/m5-learning` and resolve the conflicts so that M5 works together with everything else:
   - the decision log, permission mining, the distiller, ranking and project preparation;
   - `/update-config`;
   - background review and the curator;
   - the optimizer;
   - the proposal-kind icons in the TUI.

   M5 hooks must also see decisions from M4b's sub-agents and fan-out, and from M4c's unattended runs. Those runs must **not** record approval decisions as user decisions: tag them `actor=auto`, and have the miners use only `actor=user`.
2. **Complete the command set.** Every command from `GOAL.md` must exist and appear in `/help`:

   ```
   goal, loop, compact, bg, effort, model, ultracode, ultraplan, preview, stats, branch, clear, exit, stop,
   update, settings, export, fork, import, mcp, memory, output-style, permissions, rename, resume, skills,
   ultraresearch, review, debug, doctor, schedule, config, update-config, add-dir, artifacts, advisor
   ```

   - **`/permissions`** (missing as a slash command):
     - `/permissions` shows the mode and lists the effective rules with their source (built-in, user, project or session) and the hardline list;
     - `/permissions mode <m>`;
     - `/permissions allow|ask|deny <tool> <pattern> [--project|--user]`;
     - `/permissions rm <id>`;
     - `/permissions suggest`, from M5.
   - **`/focus`** is TUI-side. Also register it on the gateway so `/help` shows it and it toggles via `config.set display.focus`.
   - Add a test that parses GOAL.md's command list and asserts each command is registered.
3. **Reports.** Move REPORT/PROGRESS into `docs/reports/m5-learning*.md` and `merge-m5.md`.

## Acceptance (put the outputs in REPORT.md)
- `cd core && timeout 900 uv run pytest -q -o addopts="" 2>&1 | tail -3` passes with more than 580 tests. ruff and vendor_check are clean.
- TUI: `npm ci && npm run build:ink && npm run build && npx vitest run`. Only known failures and flakes are allowed; name them.
- `uv run python scripts/demo_m5.py` and `scripts/demo_ultracode.py` pass.
- Paste the `k3code slash /help` output covering all GOAL commands.
