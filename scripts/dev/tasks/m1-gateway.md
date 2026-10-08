# M1-gateway: the k3code core speaks the TUI's gateway protocol (TUI ↔ core end to end)

**Current state:**
- `core/` is the k3code Python core: provider router, agent loop, tools, permissions and CLI. Run its tests with `cd core && uv run pytest`.
- `tui/` is the vendored Ink TUI. It speaks newline-delimited JSON-RPC 2.0 to a "gateway" child process.
- `docs/tui-contract.md` maps every method, event and server request. The **M1-core** rows are the scope of this task.
- Exact payload shapes are in the Hermes reference contracts, which you may read but not modify:
  - `~/.hermes/hermes-agent/tui_gateway/contracts/*.py`
  - the generated TS at `tui/shared/gateway-contract.generated.ts`, or wherever the TUI keeps it
- How the TUI spawns and attaches to the gateway is described in `tui/src/gatewayClient.ts` (`startSpawnedGateway`) and `tui/K3_INTEGRATION.md`.

Do NOT import Hermes Python code. Re-implement the protocol in `core/src/k3code/gateway/`, matching the payload shapes exactly.

## Build

1. **`core/src/k3code/gateway/`**
   - `server.py`: an asyncio JSON-RPC 2.0 server over stdio, newline-delimited. Logs go to stderr only. On start it emits `gateway.ready` with `skin`, `change_events: []` and `replay_epoch`.
   - Implement every **M1-core method** from `docs/tui-contract.md`:
     - `session.create`, `list`, `active_list`, `resume`, `activate`, `delete`, `interrupt`, `steer`, `title`
     - `prompt.submit`
     - `command.dispatch` / `slash.exec`
     - `model.options`
     - `config.get` / `config.set`
     - `setup.status`
     - the rest of the M1-core list. Image and clipboard methods may return a clear "not supported yet" result.
   - Methods outside M1-core return JSON-RPC error `-32601`, with a message saying k3code does not support them.
   - Map the agent loop to events: `message.start`, `message.delta`, `reasoning.delta`, `tool.generating`, `tool.start`, `tool.complete` (with `duration_s`, `summary` and `result_text`, truncated), `todo.updated`, `status.update`, `message.complete` (with `usage` and `status`) and `error`. Add the optional `importance` field (`essential` / `progress` / `debug`) on every event.
   - **Approvals.** When permissions say "ask", send the server→client request `approval` (choices: once, session, always, deny) and wait for the reply. "always" persists a rule to project config. Support `request.cancel`. In headless `-p` mode, keep the current behaviour.
   - **Sessions.** Persist them in SQLite at `$K3CODE_HOME/sessions.db`: id, title, cwd, model, created/updated timestamps, messages as JSON, status. Several sessions can exist; only the active one receives prompts, and background sessions (later) run concurrently. `session.active_list` returns rows with a `state` of `working`, `needs_input`, `completed` or `failed`, which the agent strip needs.
   - **Slash command framework.** `command.dispatch` routes to a registry in `core/src/k3code/commands/`, one module per command, where each command declares its name, aliases, help text and handler. Implement now: `/model`, `/effort` (reasoning effort passed to providers where supported), `/clear`, `/compact` (summarize the conversation with the cheap model and replace history), `/rename`, `/resume` (lists sessions), `/stop`, `/exit`, `/help`. The registry must make adding the remaining commands trivial.
2. **CLI**
   - `k3code gateway --stdio` runs the server.
   - `k3code`, with no args in an interactive terminal, launches the TUI: `node <repo>/tui/dist/entry.js`, with env set so the TUI spawns `k3code gateway --stdio`. Make the minimal change in `tui/src/gatewayClient.ts`: if `K3CODE_GATEWAY_CMD` is set, spawn that command (split argv safely) instead of the Hermes Python module. Rebuild the TUI.
   - Keep the old line REPL as `k3code --repl`.
3. **Fixes from the M0 review**
   - The router must keep a provider entry that failed with `network` in cooldown for 60 s across turns, configurable, so a dead primary isn't retried every turn.
   - All logging goes to stderr. In `-p` mode, stdout carries only the final answer, or JSON with `--json`.

## Tests (pytest)
- A protocol test that spawns `k3code gateway --stdio` as a subprocess with a **fake provider**. Add `K3CODE_FAKE_PROVIDER=script.json`, which replays canned responses including tool calls, to the provider layer. The test runs `session.create`, then `prompt.submit`, and asserts the event order through `message.complete`, then checks that `session.list` contains the session.
- An approval round trip: the fake provider requests `bash`; the server sends `approval`; the test replies `deny`, and the tool result says denied. Then `always`, which writes the rule, and the next call is not asked.
- Unknown method → -32601. A `command.dispatch` for `/model` and `/clear`.
- Router network cooldown across turns.

## Acceptance (put the outputs in REPORT.md)
- `cd core && uv run pytest -q` passes everything, old and new, and `ruff check` is clean.
- `cd tui && npm run build` succeeds.
- **Live end-to-end in a pty.** Write `scripts/e2e_tui.py` using `pexpect`, added as a dev dependency. It starts `k3code` with a temp `K3CODE_HOME`, a temp project dir and the real OmniRoute config:
  ```yaml
  providers: [{name: omniroute, kind: openai, base_url: "http://<omniroute-host>:20128/v1", api_key_env: OMNIROUTE_API_KEY, models: {default: [auto/pro-coding, auto/coding-manual], cheap: auto/coding-cheap}}]
  ```
  The script waits for the input prompt, types `create a file named e2e.txt containing the word works`, approves any approval prompt, waits up to 180 s for the turn to complete, then checks that `e2e.txt` contains `works` and exits. `OMNIROUTE_API_KEY` is in the environment. Report pass/fail with the tail of the captured screen.
