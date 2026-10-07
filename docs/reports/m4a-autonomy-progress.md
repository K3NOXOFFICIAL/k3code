# PROGRESS (M4a autonomy)

## Done
- Tiers/task-kind policy, `ModelCaller`, escalation (tool errors, loop guard; cheap/fast starts), per-tier usage + `/stats`.
- Scope gate + plan-first (auto mode), `/scope`, scope log, proposals store + `/proposals`, `/preview` + `/go`, `/advisor` (+ auto critique after plan approval).
- TUI: proposal cards (a/d), plan/scope/escalation events.
- Tests: `core/tests/test_autonomy_units.py`, `core/tests/test_autonomy_gateway.py`; ruff clean; `npm run build` ok.
- REPORT.md written (acceptance outputs inside).

## Open
- Wire `advisor.review_done` + judge escalation into `/goal` once it exists; route titles/compaction via `ModelCaller`.
- Live OmniRoute `/preview` was skipped (daily quota); rerun after 2026-10-08T03:00Z.
- TUI cards are in-memory only; no handler renders `advisor.show` (text arrives via the command output).
