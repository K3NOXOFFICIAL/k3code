# M4a autonomy — report

## Built
- `core/src/k3code/routing/tiers.py` — `Tier`, `TaskKind` (14 kinds), `DEFAULT_POLICY`, `tier_for` (config `task_tiers` overrides), `tier_model_specs` (provider `tiers:` → `models.<tier>` → `models.default`), `TierRouters` (one Router per tier over shared providers/cooldowns), `Escalation` (fast→cheap→main→strong; signals `tool_errors`, `loop_guard`, `judge_not_done`).
- `core/src/k3code/routing/caller.py` — `ModelCaller`: tier-aware one-shot calls (classification, preview, advisor, compaction summary, proposer). Writes usage rows with tier + task kind; a `ChainExhausted` on a tier escalates to the next one and emits `routing.escalated`.
- `core/src/k3code/autonomy/` — `scope.py` (classifier, JSON verdict, danger floor, `scope_log.jsonl` with prompt hash + outcome), `plan_first.py` (gate, read-only strong-tier planning turn, auto-approve/ask, plan events, proposer hooks), `proposals.py` (`proposals.jsonl`, dedup key, dismiss), `preview.py`, `advisor.py` (side critique, compaction when large, `review_done` for goals).
- `core/src/k3code/commands/autonomy.py` — `/scope`, `/proposals [all|accept|dismiss <id>]`, `/preview`, `/go`, `/advisor [question|accept]`.
- Gateway (`gateway/server.py`): `_run_turn` runs the gate, builds per-tier loops, and escalates cheap-start turns (tool errors ×3 / loop guard); `_confirm_plan` for high-risk plans in auto mode; events `scope.verdict`, `plan.show`, `proposal.show`, `advisor.show`, `routing.escalated`.
- `AgentLoop`: `task_kind`, `max_tool_errors`, `escalation_reason`; re-binds the shared reliability retry wrapper to its own router at `run()` (otherwise a planning loop on `strong` hijacked the main loop's router).
- Usage db: `tier`, `task_kind` columns (auto-migrated); `/stats` prints per-tier calls/tokens and per-kind counts.
- Config: `ProviderEntry.tiers`, `Settings.task_tiers`, `Settings.autonomy` (`plan_first`, `gate_modes`, `advisor_on_plan`, `advisor_on_goal`, `advisor_modes`, `proposals`, `escalate`, `preview_timeout`, `advisor_compact_chars`).
- Fake provider: steps accept `"model"` and `"match"` filters; every call is logged in `provider.log`.
- TUI: `k3/proposalsStore.ts`, `k3/proposalCards.tsx` (card list above the agent strip; `a` accepts → sends the action as a prompt, `d` dismisses; only with an empty composer), event handlers for `plan.show`/`proposal.show`/`scope.verdict`/`routing.escalated`, event types in `shared/gateway-events.ts`.

## Verification
- `cd core && uv run pytest -W ignore` (output redirected to a file; piping hangs because the suite leaves child processes holding the pipe open) → `330 passed in 8.43s`, rc=0. New: `tests/test_autonomy_units.py` (21), `tests/test_autonomy_gateway.py` (23, incl. escalation on tool errors and on the loop guard).
- `uv run ruff check src tests` → All checks passed.
- `cd tui && npm ci --prefer-offline && npm run build` → built `dist/entry.js`; `npx tsc --noEmit` clean; `npx vitest run src/__tests__/k3` → 37 pass (new `proposals.test.ts` 3), `agentStrip.test.tsx` cannot load in this worktree (`packages/hermes-ink/dist` not built; unrelated to this change).
- `/stats` from a fake-provider session (classification, plan, advisor critique, main turn, proposer, `/preview`, `/advisor`):
```
Usage per session:
- 7e335b24649143b9: tokens 139/88 in/out, cost unknown, calls 8 [t/m-cheap×3, t/m-fast×1, t/m-main×1, t/m-strong×3], failovers 0, retries 0, pauses 0 (0s paused), tools 1, approvals 0
    tiers: cheap 3 calls (11/3 tok), fast 1 calls (8/40 tok), main 1 calls (50/15 tok), strong 3 calls (70/30 tok)
    kinds: advisor×2, classification×3, interactive_turn×1, plan×1, preview×1
```
- Live `/preview` on OmniRoute: **skipped** — the API key is at its daily quota (HTTP 429 `usage_limit_exceeded`, `retry_after`≈59826 s, resets 2026-10-08T03:00Z). The attempt took 30.1 s: the router honours that Retry-After as a sleep on the first chain entry, so the 30 s `/preview` budget fired before any failover and the command returned its timeout message instead of hanging (its "try a shorter task" hint is wrong for this cause).

## Deviations / notes
- Commit trailer is `Co-Authored-By: Claude Sonnet 5.5` (the harness attribution reminder), not the `Opus 5.5` the task text names.
- `advisor.show` is emitted but the TUI has no handler for it; the critique reaches the user via the command's `output`.
- The scope gate runs in `auto` mode by default (`autonomy.gate_modes: [auto]`); add `default` to run it there too, which uses the existing `exit_plan` approval. `/scope <level>` forces the gate in any mode for the next task. Danger classes still force a high-risk plan after an override.
- Auto mode now spends a cheap classification call per task; `autonomy.plan_first: false` turns the whole gate off. One older test (`test_auto_mode_logs_side_effects…`) sets that, since its scripted turns assumed no classifier call.
- Fixed an existing bug on the way: the turn passed the config model *key* (`"default"`) as the model name to the provider; the chain is already built from the key, so `model=` is no longer passed.
- Only tiers below `main` escalate on loop failures (cheap/fast start → next tier up); interactive `main` turns keep the old "stopped, needs input" behaviour.
- No `/goal`, titles or real compaction exist in this tree (M1 remaining commands are not merged here). The hooks are ready: `advisor.review_done()` (blocking issues keep the goal going), `Escalation.record("judge_not_done")`, and `TaskKind.GOAL_JUDGE/TITLE/COMPACTION` routes. `/advisor` summarises large conversations through the `compaction` kind.
- TUI `a`/`d` are plain keys as specified, so while cards are visible the first letter `a`/`d` of an empty composer acts on a card instead of typing.
- Large/huge tasks: plan is produced, `fanout_candidate=true` is logged and shown, execution is sequential (M4b).

## Open TODOs
- Wire `review_done` into `/goal` and the judge escalation once `/goal` lands; route titles/compaction through `ModelCaller`.
- Persist a card's pending/accepted state on TUI reconnect (cards are in-memory; `/proposals` is the durable view).
- Re-run the live OmniRoute `/preview` after the quota resets.
