# M3-panes: integrate k3code with `k3` panes (agent states, `/bg --pane`, Inbox approvals)

## Current state

- `panes/` is the vendored tuios (Go) with the k3 keymap: `internal/k3keys` and `cmd/k3`. Read `docs/reports/m3-keys.md`.
- tuios already supports agent-aware panes:
  - harness manifests in `panes/internal/harness/manifests/*.toml`, embedded with `go:embed`, which detect agent CLIs and their states;
  - an agent-state reporting protocol (`internal/agentproto`, a `set-agent-state` verb on the tuios socket `$TUIOS_SOCKET` with `$TUIOS_PANE_ID`);
  - integration hooks for other harnesses in `internal/integration/` (e.g. `assets/hermes/…`);
  - an Inbox for approvals;
  - `tuios fan` for worktree fleets.

  Read those files to learn the exact protocol. **Do not** guess it.
- The k3code core is in `core/`. The gateway emits session states (`working` / `needs_input` / `completed` / `failed`) and approval requests.

## Build

1. **`panes/internal/harness/manifests/k3code.toml`**: detect `k3code` / `k3code-tui` processes in panes, plus resume/input rules modelled on the existing manifests, such as the claude, opencode and hermes ones.
2. **State reporter: `core/src/k3code/integrations/panes.py`.**
   - When `$TUIOS_SOCKET` and `$TUIOS_PANE_ID` are set, the gateway reports the active session's state to tuios via the documented verb, mapped onto tuios' state vocabulary: working, waiting for input, done, error.
   - Report on every state change. If the socket isn't there, do nothing.
   - Also add an integration target in `panes/internal/integration/` (like the hermes one) if tuios needs one.
3. **`/bg --pane <prompt>`.** When running inside k3 panes, start the background session in a **new pane**, running `k3code` attached to the daemon session, through the tuios socket's start/spawn verb. Outside panes, fall back to a normal `/bg`.
   - Also add `/fork --pane`.
   - Fan-out children with `autonomy.fanout.panes: true` open one pane each, read-only attached.
4. **Inbox approvals.**
   - When a k3code session in a pane is waiting for approval and the user isn't focused on that pane, the approval appears in the tuios Inbox (through the verb tuios uses for ask-human/approvals).
   - Answering in the Inbox resolves the gateway approval: once, session, always or deny.
   - It is fine if only once/deny are supported by the Inbox protocol; document it.
5. **`k3 --help` and docs.** Update `panes/docs/k3-keymap.md` with an "Agents" section: the agents/inbox mode (`Ctrl+G a`), pane badges and the Inbox.

## Tests
- Go: the manifest parses and the detection matches the k3code process cmdline (use the existing manifest test pattern).
- Python: the reporter sends the correct verbs and payloads over a fake Unix socket; state mapping; no-op without env.
- `/bg --pane` sends the spawn verb (fake socket), and falls back to `/bg` when not in panes.
- Inbox round trip: an approval request is sent to the socket, and the fake socket answers `deny`, which resolves the gateway approval as denied.

## Acceptance (put the outputs in REPORT.md)
- `cd panes && go build ./cmd/k3 && go test ./internal/k3keys/... ./internal/harness/...` passes.
- `cd core && timeout 900 uv run pytest -q -o addopts=""` passes, and ruff is clean.
- **Live, if feasible headless:** start `k3` in a pty with `--headless` or a test harness if tuios supports one, open a pane running `k3code` with the fake provider, and show that the pane's agent state changes to working, then done, via the tuios CLI or socket query. If it is not feasible, explain why, and provide the manual test steps in `docs/k3-panes-test.md`.
