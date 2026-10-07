# M1-tui Progress

## Done
- ✅ Cut billing, free tier, subscription, top-up, connectors, vault, bot relay, hosted rooms, pets/hatch, Nous login/onboarding from menus/overlays/slash-command lists/status bar
- ✅ Deleted components no longer imported: billingOverlay, connectionSetupOverlay, petPicker, petSprite, subscriptionOverlay, petFlashStore consumers, wakeState, connectionOperationStore, billingDialog, petPolling, slash commands (subscription, topup, wake)
- ✅ Rebranding complete: brand name/icon (theme.BRAND), k3code text banner + hero (banner.ts, branding.tsx), all user-visible `hermes` CLI references (`k3code doctor/setup/model/update/--tui/plugins/-c/edit`), lifecycle + crash messages, heapdump/editor/tmp paths (`.hermes` → `.k3code`), `HERMES_*` env vars → `K3CODE_*` (38 names, 19 src + 12 test files), skins dir `~/.k3code/skins`
- ✅ `K3CODE_GATEWAY_CMD` spawn override in gatewayClient.ts (shell-style argv split; falls back to `python -m tui_gateway.entry`) — matches m1-gateway launcher
- ✅ `lib/history.ts` rewritten: per-project input history at `K3CODE_HOME/projects/<flat-cwd>/.input_history` (spec §3)
- ✅ `tui/src/k3/focusPolicy.ts` created (importance-aware filter table)
- ✅ `lib/agentRows.ts` pure row-builder (ink-free, shared with LiveAgentsPanel)
- ✅ Stale tests fixed (usageCommand rewritten for cut billing panel; slashParity probes k3code core registry; hermes strings in tests updated)
- ✅ Typecheck clean; full suite 170 files / 1544 passed / 2 skipped (0 failures — the 2 known upstream flakes from m0 now pass)

## In Progress
- ⏳ Create `tui/src/k3/agentStrip.tsx` (agent strip below composer): LiveAgentsPanel data source, ≤6 rows + "+N more", hidden when empty, keyboard nav (↓ into strip on empty input, ↑/↓ rows, Enter attach, Esc back, x stop w/ confirm, Ctrl+B background)
- ⏳ Mount strip below composer in appLayout.tsx (replace LiveAgentsPanel)
- ⏳ Wire input history ↑/↓ priority vs strip nav in useInputHandlers.ts
- ⏳ Wire focus mode: /focus slash command, Ctrl+F binding, FOCUS badge in status line, transcript filtering via focusPolicy, `display.focus_mode` persist (config.set + memory fallback)
- ⏳ Write new tests: agentStrip states/keyboard/+N; history cycling + strip interaction; focusPolicy type×importance table; cut-feature bundle grep on dist/entry.js
- ⏳ npm run build + bundle grep + REPORT.md + commit

## Next
- Build agentStrip.tsx, then wire keyboard precedence (history when input non-empty or cycling; strip only on empty input)
- Wire focus mode end-to-end
- New tests, build, REPORT.md
