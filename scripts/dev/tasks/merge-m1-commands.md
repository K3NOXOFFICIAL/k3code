# merge-m1-commands: merge the `w/m1-commands` branch into this branch, resolve conflicts, keep everything working

The current branch already contains M2-ops, which:
- refactored the gateway into multi-session, multi-client form with a Unix-socket daemon;
- added the reliability events, doctor, stats, debug, sandbox and `/model chain`.

The branch `w/m1-commands` was developed in parallel from an older base. It adds:
- commands: export/import, fork, branch, settings, config, output-style, memory, skills, mcp, review and goal;
- service wiring in `gateway/server.py`;
- config changes;
- CLI subcommands.

Read `docs/reports/m2-ops.md` and the `REPORT.md` on that branch (`git show w/m1-commands:REPORT.md`).

## Do
1. Run `git merge --no-ff w/m1-commands`. Expect conflicts in `core/src/k3code/cli.py`, `core/src/k3code/config.py` and `core/src/k3code/gateway/server.py`; there may be others.
2. Resolve each conflict so that **both** features work. M2-ops' multi-client architecture is the base: the commands' services must plug into it, e.g. per-session state, events routed to a session's clients, and the attach flow. Do not drop any command, CLI subcommand, config field or test from either side.
3. Move `REPORT.md` and `PROGRESS.md` (from the merged branch) to `docs/reports/m1-commands.md` and `docs/reports/m1-commands-progress.md` with `git mv`.
4. Fix the security issue M2-ops reported: `config.get full` returns provider `api_key` values to the client. Redact them to `"<redacted>"` in that response, and add a test.
5. Make the whole test suite pass:
   - `cd core && uv run pytest -q -o addopts=""`; run long tests with `-x` first. The full suite may take more than 5 minutes; use `timeout 1200`.
   - `uv run ruff check src tests ../scripts`
   - `cd tui && npm ci && npm run build:ink && npm run build`
   - `npx vitest run`: only the known `textInputFastEcho` failure is allowed.
6. Commit the merge. The commit message must list the resolution decisions per file.

## Acceptance (put the outputs in REPORT.md at the repo root)
- The pytest pass count, which should be roughly the sum of both sides minus duplicates (more than 300).
- ruff is clean, and the TUI build and vitest results.
- A list of each conflict and how it was resolved.
