# merge-m1-commands — report

Merged `w/m1-commands` into the M2-ops branch (`git merge --no-ff`).

## Conflicts and resolutions
- `core/src/k3code/config.py` — one hunk (user config path). Took M1's `_current_home()` (reads `K3CODE_HOME` at call time, same behaviour as M2's inline version). All M1 config sections (`mcp skills mem0 display goal output_style permissions`) kept.
- `core/src/k3code/cli.py` — both sides appended commands at the same spot; kept both (M2: `daemon`, `service`, `doctor`, `stats`; M1: `export`, `import`, `memory`, `config-edit`).
- `core/src/k3code/gateway/server.py` — 5 hunks:
  - `LiveSession.__init__`: M1's typed `control` dict plus goal snapshot restore, then M2's background/reliability/paused/needs_input state and `emit()`.
  - `GatewayServer.__init__`: M2's `live` dict (M1's single `self.session` field dropped; M2's `session` property/setter over the current client replaces it) plus M1's `mcp` manager and `goal_judge` hook.
  - `close()`: M2's per-session turn cancel and reliability stop, plus `await mcp.close()`.
  - `_run_turn`: M1's goal loop wrapper around `_run_one_turn`; it sets the `_ctx_session` contextvar, and goal notifications use `session.emit` so they reach only that session's clients. `_run_one_turn` keeps M2's reliability handling, `needs_input` reset and `state` in status/complete events, and M1's `mcp.ensure_started()` and per-turn `build_system_prompt`.
  - Services for commands: `activate_session` now uses `live_for()` + the session setter (attaches the requesting client, emits `session.info` to the session's clients) instead of replacing the single session; `emit_goal` routes through `session.emit`.
- Auto-merged files (`commands/builtin.py`, `tools/__init__.py`) needed no change; no command, CLI subcommand, config field or test dropped.

## Other changes
- `REPORT.md`/`PROGRESS.md` from M1 moved to `docs/reports/m1-commands.md` and `docs/reports/m1-commands-progress.md` (`git mv`).
- Security fix: `config.get` with `key=full` now returns `redact(config.model_dump())`; provider `api_key` becomes `"<redacted>"` (`api_key_env` kept). Test: `tests/test_cmd_config.py::test_rpc_config_get_full_redacts_api_keys`.
- One ruff import-order autofix in `server.py`.

## Verification
```
cd core && uv run pytest -q -o addopts=""        → 321 passed (M2 side 286 + M1 side 285 minus shared + 1 new)
cd core && uv run ruff check src tests ../scripts → All checks passed!
cd core && uv run python ../scripts/vendor_check.py → All checks passed
cd tui && npm ci && npm run build:ink && npm run build → built dist/entry.js
cd tui && npx vitest run → 1 failed (textInputFastEcho › colorizeEcho, the known env failure), rest pass.
```
One earlier vitest run also failed a scroll-position test (adjustScrollTop, `useVirtualHistory`-style); it passed on rerun, so it is load-dependent flakiness (the run took 106 s).

## Deviations / open TODOs
- No live-model e2e or TUI→daemon e2e rerun after the merge (OmniRoute quota); covered by unit/gateway tests only.
- `_ensure_router` is still server-global (M2 TODO); M1's `_router_for` one-shot router cache is also server-global.
- Pytest shows an "Event loop is closed" ResourceWarning from an MCP stdio subprocess at teardown (warning only).
