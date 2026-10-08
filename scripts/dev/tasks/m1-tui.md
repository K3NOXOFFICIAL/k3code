# M1-tui: make the vendored TUI k3code's own (cut, rebrand, agent strip, ↑ history, focus mode)

`tui/` is the vendored Hermes Ink TUI (MIT). It builds with `cd tui && npm install && npm run build`, and its tests run with `npm run test`. `docs/tui-contract.md` maps every gateway method and event and classifies each as M1-core, later or cut. Read `tui/K3_INTEGRATION.md` first.

Do only TUI (TypeScript) work. A separate worker builds the Python gateway.

## 1. Cut and rebrand
- Hide or remove every UI surface for **cut** features: billing, free tier, subscription, top-up, connectors, vault, voice, wake word, bot relay, hosted rooms, pets/hatch, Nous login and Nous-specific onboarding.
  - Prefer removing them from menus, overlays, slash-command lists and the status bar.
  - Delete a component only when nothing else imports it.
  - Make the TUI never call cut gateway methods.
- Rebrand all user-visible strings and the logo/banner from Hermes to **k3code**.
  - Keep the MIT notices and the attribution in `tui/LICENSE`.
  - Add a simple text banner `k3code`, with a version from package.json.
- Keep "later" features (goal bar, loops, cron, todo panel). They will be wired up later.

## 2. Agent strip under the input (Claude-Code-like multi-session view)
- Create `tui/src/k3/agentStrip.tsx`, rendered **directly below the composer/input box**.
  - Hermes' `LiveAgentsPanel` currently mounts above the composer (`appLayout.tsx`). Reuse its data source (`session.active_list`, subagent events, `lib/agentRows`) and move or replace it.
- One row per background session or sub-agent. Each row shows:
  - a status glyph: `◐ working`, `● needs input` (approval, clarify or question pending), `✓ completed`, `✗ failed`;
  - the title;
  - the elapsed time;
  - a short last-activity line.
- Show at most 6 rows, then `+N more`. Hide the strip when it's empty.
- **Keyboard:**
  - When the input is **empty**, `↓` moves focus into the strip, `↑`/`↓` move between rows, `Enter` attaches to (activates) that session, and `Esc` or `↑` past the first row returns to the input.
  - `x` on a row stops that agent, after asking for confirmation.
  - `Ctrl+B` on a running foreground turn sends it to the background (`/bg` semantics) if the contract supports it; otherwise add a TODO.

## 3. Input history
- `↑` in the input field, when the cursor is on the first line, cycles previous submitted inputs, with persistence per project.
- `↓` goes forward.
- Check what Hermes already has (`useInputHistory` or similar) and make sure it works together with the strip navigation from section 2. History takes priority when the input is non-empty or a history cycle is in progress.

## 4. Focus mode
- Add `tui/src/k3/focusPolicy.ts`, toggled with `Ctrl+F` and also with `/focus`.
- When focus mode is on, the transcript shows only:
  - user messages;
  - questions, clarifications and approvals (anything that needs input);
  - errors and warnings;
  - the final assistant answer of each turn;
  - notifications marked important.
- When focus mode is on, it hides tool calls and output, thinking/reasoning, interim messages, todo churn and status chatter.
- Show a small `FOCUS` badge in the status line.
- Events may carry an optional `importance: "essential" | "progress" | "debug"` field. Honour it when present, and fall back to the event-type rules above otherwise.
- Persist the setting in config (`display.focus_mode`) when `config.set` is available. Otherwise keep it in memory only.

## 5. Tests (vitest)
- agentStrip: rendering of each state; keyboard navigation (empty-input ↓, ↑/↓, Enter, Esc); `+N more`.
- History: ↑/↓ cycling, and the interaction with the strip.
- focusPolicy: a table test of event type × importance → shown or hidden.
- Cut features: no cut method names appear in the built bundle. Grep `dist/entry.js` in a test or script.

## Acceptance (put the outputs in REPORT.md)
- `npm run build` succeeds.
- `npm run test` reports no new failures beyond the 2 known upstream ones listed in `docs/reports/m0-tui.md`.
- New tests pass.
- `grep -c -iE 'billing|free_tier|subscription|vault\.|bot_relay|hatch' tui/dist/entry.js` is 0, or every remaining hit is explained.
- Include a screenshot-like text dump of the strip, produced with ink-testing-library render output.
