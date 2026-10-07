# M1-permissions report

## Built
- `core/src/k3code/permissions/` (package replaces `permissions.py`): `wildcard.py`, `arity.py`, `rules.py`
  (opencode port @ ecc4916b, in `VENDOR.toml`), `hardline.py` (extendable via `permissions.hardline` in config),
  `engine.py` (`decide()`, modes default/accept-edits/plan/auto/yolo, `suggest_rules`), `state.py`
  (`PermissionState`, config load, `persist_rules` → project config, `log_decision` → `$K3CODE_HOME/decisions.jsonl`),
  `check_permission` kept as compat wrapper.
- `agent/loop.py`: uses the engine; `ask` → approval callback; deny → `User denied: <cmd> — <reason>`; `exit_plan`
  intercepted (offered to the model only in plan mode); auto-allowed side effects reported via callback.
- Gateway: real approval round trip (once/session/always/deny, suggested pattern in request), `session.mode.cycle|set`,
  `session.info` with `mode`/`approval_mode`, `config.set yolo` maps to mode, per-session `add_dirs`/mode persisted in
  sessions.db (`meta` column), `/add-dir`, `permission.auto_allowed` events. Fixed: the approval callback was an
  un-awaited coroutine; `ctx.sessions` did not exist for commands.
- Tools resolve relative paths against the session cwd; the old hard traversal guard moved into the engine (roots + approvals).
- TUI: Shift+Tab → `session.mode.cycle`, mode shown in status line, approval prompt supports per-request labels (plan approval).
- Tests: `test_wildcard/arity/rules/hardline/engine.py`, `test_permissions_gateway.py` (once, session, always + new session,
  deny, hardline in yolo, mode cycle, plan→exit_plan, cwd, add-dir, auto event log). conftest isolates `K3CODE_HOME`.

## Verification
- `cd core && uv run pytest -q` → all pass (115 tests); `uv run ruff check .` → clean.
- `cd tui && npm run build` → built dist/entry.js; vitest approvalAction + appChrome → 40 passed.
- `python3 scripts/vendor_check.py` → all checks passed.
- Live e2e with fake provider (`K3CODE_FAKE_PROVIDER`, bash `echo bashworks > ran.txt`):
  `approval-prompts-answered=2`, `RESULT PASS 'bashworks\n'` (before the fix: 0 prompts).

## Deviations / TODO
- **Live e2e against the real model could not run**: the OmniRoute key returned 429 `usage_limit_exceeded` (daily quota,
  resets 2026-10-08 03:00Z) for every model. The same TUI→gateway→tool path was run with the scripted fake provider instead.
  TODO: rerun the exact command from the task once quota resets.
- `always`/`session` patterns are `<arity prefix> *` (e.g. `git commit *`), not `git commit*`, so `sh` can't approve `shutdown`.
- `always` for write/edit persists the exact file path. Reads/writes outside roots ask (not hard-deny) so they can be approved.
- Builtin bash allow is downgraded to ask when the sub-command has redirects or `$(...)`/backticks.
- Specificity first, then later source wins (session > project > user > builtin), then deny > ask > allow.
- Plan mode denies even `ls` in bash (spec: pure tools + exit_plan only).
