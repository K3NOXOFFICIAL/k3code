# M3-panes report

## What was built
- **Merge**: `w/merge-m4b` merged first (this branch had no `/bg`/fan-out to extend).
- `panes/internal/harness/manifests/k3code.toml`: detects `k3code` / `k3code-tui` (comm, argv0, package-dir `k3code`),
  resume `k3code attach {session_id}`, input rules. No screen rules (k3code reports its own state; a bundled screen
  rule needs a measured fixture).
- `panes/internal/k3keys`: Agents mode (`Ctrl+G a`) now has `i` Inbox, `n` next waiting pane, `s` agent settings; hints strip
  updated; `cmd/k3` gains `-h/--help` with an Agents section; `panes/docs/k3-keymap.md` has an "AGENTS" section
  (badges, Inbox, approvals config, `/bg --pane`).
- `core/src/k3code/integrations/panes.py`: `TuiosSocket` (verb socket client), `PaneReporter` (ordered, deduped,
  off-thread `set-agent-state`, harness `k3code`), `PaneLink` (frame tap), `open_pane_spec`/`k3code_argv`.
  State map: working→working, needs_input→needs_input, completed→done, failed→errored, idle→idle, and idle right after
  a working/needs_input turn → `done` (what Claude's Stop hook sends). No `TUIOS_SOCKET`/`TUIOS_PANE_ID` → `from_env` returns None.
- Wiring: `gateway/server.py` (stdio gateway taps its own frames, `_pane_inject` resolves requests the Inbox answered),
  `daemon.attach_bridge` (the process that actually sits in the pane when the TUI uses the daemon; also `--readonly`).
- `/bg --pane`, `/fork --pane`: handler starts the session and returns `open_pane`; the in-pane process (stdio gateway or
  attach bridge) sees it and sends `start-agent` (`k3code attach <id>`). Outside panes nothing taps it, so it is a plain `/bg`/`/fork`.
- Fan-out: `autonomy.fanout.panes` (default false) emits `pane.open`; one `k3code tail <subagent>` read-only pane per child.
- CLI: `k3code attach <id> [--readonly]`, `k3code tail <subagent-id>`, `gateway --attach --readonly`; `subagent.tail` also returns `done/status`.
- Inbox approvals: gateway `approval` request → `set-agent-state needs_input kind=approval` + blocking `request-approval`
  (summary, tool, target, options once/always/deny, always_scope = the rule pattern). Inbox decision → `{"choice":...}` injected as the TUI's
  answer, plus `request.cancel` so the TUI's prompt closes. Answering in the pane first closes the hold. No answer (pane focused/timeout) → pane prompt decides.
- Tests: `core/tests/test_panes.py` (20) with `tests/fake_tuios.py`; Go: `TestK3codeProcessDetection`, `TestAgentsModeKeys`.
- `docs/k3-panes-test.md`, `core/scripts/live_panes_demo.py`.

## Verification
```
cd panes && go build ./cmd/k3 && go test ./internal/k3keys/... ./internal/harness/...   -> ok (k3keys, harness, herdrconv)
cd core && timeout 900 uv run pytest -q -o addopts=""                                  -> 535 passed
cd core && uv run ruff check .                                                           -> All checks passed
```
**Live** (isolated tuios daemon, own HOME/XDG_RUNTIME_DIR; steps in `docs/k3-panes-test.md`): a pane running a real
`GatewayServer` (fake provider) with the tap; polling `tuios list-agents -s live4`:
```
t+0.5s  None
t+3.5s  working | thinking
t+7.5s  done
```
and `tuios list-attention` listed the finished turn in the Inbox. The pane's tuios daemon is separate from the user's.

## Deviations / notes
- Inbox has no "session" decision: Inbox = once / always / deny; "session" exists only in the pane's own prompt (documented).
  `always` is only offered when the request carries a rule pattern.
- Needs `[agents.approvals] enabled = ["k3code"]` in the tuios config (tuios default is off); not auto-written.
- `k3` (the local variant) has no daemon socket; the pane integration needs a tuios daemon session (`TUIOS_SOCKET` set).
- Read-only attach is enforced in the bridge (a method deny-list), not in the daemon.
- Fan-out children are sub-agents, not sessions, so their pane is `k3code tail` (polls `subagent.tail` on the daemon), which needs the daemon.
- `idle` after a turn is reported as `done`.

## Open TODOs
- Not exercised live: real Ink TUI + node in a pane, a live Inbox answer (`reply-approval` needs the person's nonce), `/bg --pane` against a real tuios.
  Covered with a fake tuios socket instead.
- No-op `status.update` state "paused"/"new" is not mapped.
- Optional: ship a tuios integration-installer target for k3code (not needed: the gateway reports directly).
