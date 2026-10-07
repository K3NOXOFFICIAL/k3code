# M1-permissions — progress

Task: real permission engine with modes + approvals (opencode-style rules).
Branch: `w/m1-permissions`. Bug: `check_permission` allows everything in
interactive ask mode, so TUI approval prompts never fire.

## Done
- Reference ported (opencode @ ecc4916b): wildcard, arity, index (+ tests read).
- vendor_check format checked (sha mismatch is WARN-only).
- permissions package: `wildcard.py`, `arity.py` + tests, VENDOR entries.

## In progress
- `rules.py` (Rule, from_config, evaluate).

## Next
- `hardline.py`, `engine.py` (modes + decide + bash split + roots).
- Wire `AgentLoop`, compat `check_permission`, config `permissions:` merge.
- Gateway: mode cycle/set, session.info, approval round trip (once/session/
  always/deny), decisions.jsonl, /add-dir. exit_plan tool.
- Mode×tool + gateway round-trip + plan + cwd tests.
- TUI: Shift+Tab cycle + status mode.
- Acceptance: pytest + ruff, tui build, live e2e (approval-prompts>=1, PASS).
- REPORT.md, commit it.
