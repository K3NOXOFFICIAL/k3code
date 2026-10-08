# M0-tui: vendor the Hermes Ink TUI into `tui/` and map its gateway contract

k3code's terminal UI reuses Hermes Agent's Ink/React TUI. The new Python core (`core/`, built separately) will implement the subset of the gateway JSON-RPC contract that the TUI needs.

Reference checkout, read-only: Hermes Agent at `~/.hermes/hermes-agent`, MIT, commit 4127d78da84b1eee105f298979cc57cc7457f98d.
- The TUI is `ui-tui/`, including `ui-tui/packages/hermes-ink`.
- The shared TS protocol code is `apps/shared/src`.
- The Python gateway is `tui_gateway/`, with its contracts in `tui_gateway/contracts/*.py`.

## Do this

1. **Copy the TUI.**
   - Copy `ui-tui/` → `tui/` and the parts of `apps/shared/src` that the TUI imports → `tui/shared/`.
   - Exclude `node_modules`, `dist` and build caches.
   - Fix the import paths and the workspace/package config so `tui/` builds standalone with npm (Node 22 is installed; no bun).
   - Rename the package to `@k3code/tui` and the binary/entry to `k3code-tui`. Keep `hermes-ink` as an internal package, renamed `@k3code/ink`.
   - Keep the MIT notice: add `tui/LICENSE` with Hermes' MIT text and a `NOTICE` line "Derived from NousResearch/hermes-agent ui-tui @4127d78".
   - Add one VENDOR.toml `[[tree]]` entry for the whole directory (`project`, `upstream_path`, `commit`, `license`, `local_path`) rather than per-file entries. If `VENDOR.toml` doesn't exist, create it with just this entry; another worker may add `[[file]]` entries, and the merge will be resolved later.

2. **Build.** Run `npm install` (or `npm ci` if a lockfile is usable) inside `tui/`, then `npm run build` (or the equivalent `tsc`/bundle step) until it succeeds. Run the existing vitest tests and record pass/fail counts. Don't spend long fixing tests that fail upstream too; list them.

3. **Write `docs/tui-contract.md`**, the contract map:
   - every JSON-RPC method the TUI calls (there are about 121; the server side registers them with `@method("name")` in `tui_gateway/*.py`), with params and result shape taken from `tui_gateway/contracts/*.py` or the handler;
   - every server→client event/notification the TUI handles, such as `message.delta`, `tool.start`, `tool.complete` and approval requests;
   - the transport: stdio child process vs WebSocket, framing, handshake, and how the TUI launches or attaches to the gateway (env vars, CLI args).
   - Classify each item as:
     - **M1-core**: needed for chat, streaming, tools, approvals, sessions, model, config, slash commands and sub-agent/agents panel.
     - **later**: goals, loops, cron and other features k3code will have later.
     - **cut**: Nous-specific features to remove or stub: billing, free_tier, subscription, connectors, vault, voice, wake, bot_relay, hosted rooms, pets.
   - For **cut** items, list which TUI components or overlays reference them, so they can be hidden.

4. **Write `tui/K3_INTEGRATION.md`**, explaining how to launch the built TUI against a custom gateway command. The goal is `k3code` → spawns `k3code-tui`, and the TUI spawns or attaches `k3code gateway --stdio`. Name the exact place in the TUI code where the gateway command is configured.

## Acceptance (put the outputs in REPORT.md)

- `cd tui && npm run build` succeeds, and the built entry exists.
- vitest pass and fail counts are recorded.
- `docs/tui-contract.md` exists and covers every method, with the counts per class (M1-core / later / cut).
- No `node_modules`, `dist` or other build output is committed. Check `.gitignore`.
