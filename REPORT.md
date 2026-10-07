# M5-learning report

## Built
- `core/src/k3code/learning/`: decisions.py (SQLite log + jsonl migration), permrules.py (approvals_suggest port), distiller.py, ranking.py, projectprep.py, updateconfig.py, review.py + curator.py (Hermes ports), optimizer.py (A/B overlays, auto-rollback), hub.py (wiring).
- `core/src/k3code/commands/learning_cmd.py`: /permissions suggest, /update-config, /optimizer, /self-improve, /learn.
- TUI: kind icons for permission_rule, preference, project_setup, skill, optimizer, consequence, improvement (+ fallback) in `tui/src/k3/proposalsStore.ts`, `proposalCards.tsx`.
- Tests: `core/tests/test_learning_*.py`; demo: `scripts/demo_m5.py`.
- VENDOR.toml: four Hermes ports recorded.

## Verification
- `cd core && timeout 900 uv run pytest -q -o addopts="" | tail -3` → 524 passed.
- `uv run ruff check .` → All checks passed.
- `python3 scripts/vendor_check.py` → all entries OK.
- TUI: `npm run build` OK. `npx vitest run` → 926 tests passed; 68 test files fail to import `hermes-ink/dist/entry-exports.js` because `tui/node_modules` links to another worktree whose hermes-ink dist is unbuilt (environment issue, the known failure; not a test failure).
- Demo (`cd core && uv run python ../scripts/demo_m5.py`):
```
1. approved `npm test` in 3 separate sessions
2. proposal [permission_rule]: You always allow `npm test *` in this project (3×) → add an allow rule?
3. accepted -> allow rule `npm test *` written to <tmp>/proj/.k3code/config.yaml
4. proj/.k3code/config.yaml:
permissions:
  bash:
    npm test *: allow

5. new proposal: You keep denying `terraform apply *` in this project (2×) → add a deny rule?
6. dismissed it
7.1 proposer rerun -> 0 new proposals (statuses: ['accepted', 'dismissed'])
7.2 proposer rerun -> 0 new proposals (statuses: ['accepted', 'dismissed'])
OK
```

## Deviations
- Vendor entries mark sha256_upstream of the Hermes originals; files are design ports (modified = true).
- A second session continued the first; I did not re-review earlier modules beyond running the full suite.

## Open TODOs
- /self-improve is a stub (issue draft only), as specified.
- No live-model run of distiller/review/update-config; fake provider only.
- vitest needs a built hermes-ink in the linked node_modules to run the 68 import-failing files.
