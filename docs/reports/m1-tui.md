# M1-tui report

## Built
- Cut/rebrand (earlier session, committed): billing, free tier, subscription, top-up, connectors, vault, voice, wake, bot relay, hosted rooms, pets, Nous login removed from menus/overlays/slash lists/status bar; unused components deleted; Hermes→k3code strings, `K3CODE_*` env vars, text banner `k3code` (version from package.json); `tui/LICENSE` kept.
- `tui/src/k3/agentStrip.tsx` (view + row builder + connected `AgentStrip`) and `tui/src/k3/agentStripStore.ts` (stores, pure key reducer `reduceStripKey`, `shouldEnterStrip`). Mounted directly below the composer in `components/appLayout.tsx` (replaces `LiveAgentsPanel`). Data: `session.active_list` poll (published from `useMainApp.ts`, current session excluded) + `useAgentRoster()` sub-agents. ≤6 rows then `+N more`; hidden when empty. Glyphs `◐ ● ✓ ✗`.
- Keys (`app/useInputHandlers.ts`): ↓ on empty input with no history cycle focuses strip (TextInput gets `focus={false}`); ↑/↓ move, Enter → `session.activate`, Esc or ↑ past first row returns, `x` → `y/n` confirm → `subagent.interrupt` / `session.interrupt`. Double-Esc detection skips while strip focused.
- History: `lib/history.ts` per-project persistence (`K3CODE_HOME/projects/<cwd>/.input_history`); existing ↑/↓ cycling kept. Priority: strip entry requires `input === ''` and `historyIdx === null`, so history always wins when non-empty/mid-cycle.
- Focus mode: `k3/focusPolicy.ts` (`shouldShowInFocusMode` event table honouring `importance`; `focusVisibleMessages` transcript filter; `toggleFocusMode`). `Ctrl+F` and `/focus [on|off|status]` toggle `focusView`; transcript hides non-essential rows, todo panel and live tool/thinking sections; existing `FOCUS` badge in status line. Persisted via `config.set {key:'focus'}` (best effort; in-memory otherwise).
- Tests: `src/__tests__/k3/{agentStrip.test.tsx,focusPolicy.test.ts,inputHistory.test.ts,cutFeatures.test.ts}`.

## Verification
- `cd tui && npx tsc --noEmit -p .` → clean.
- `npm run build` → success (`dist/entry.js`).
- `npm run test` → 174 files, 1589 passed, 2 skipped, **1 failed**: `textInputFastEcho > passes through on a non-color value` (known upstream failure from docs/reports/m0-tui.md; colour-level dependent, passes/fails by env). New tests: 34 + 12 pass.
- `grep -c -iE 'billing|free_tier|subscription|vault\.|bot_relay|hatch' dist/entry.js` → 7 lines, all benign: provider-error classification text ("billing" in model-provider error categories) and React's "subscription" warning strings. `cutFeatures.test.ts` greps quoted method names (`billing.`, `vault.`, `wake.`, `pet.*`, …) → none.

Strip dump (ink render):
```
  ◐ task 1 1m 6s · working · doing step 1
  ● task 2 1m 7s · needs input · doing step 2
  ✓ task 3 1m 8s · completed · doing step 3
  ✗ task 4 1m 9s · failed · doing step 4
```

## Deviations / TODOs
- Ctrl+B (foreground → background): contract has no handoff (only `prompt.background` for new tasks); TODO comment in `useInputHandlers.ts`.
- Focus persistence uses the gateway's existing `config.set` key `focus`, not `display.focus_mode`; gateway worker should map it.
- Approval/clarify requests are overlays (always shown), so they are not in the event table.
- Interactive behaviour of the strip/focus in a real terminal against the live gateway was not run; covered by unit/render tests only.
- "needs input" for sub-agents is not available (only sessions with status `waiting`).
