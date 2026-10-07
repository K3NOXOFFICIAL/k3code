# M2-ops: run k3code 24/7, plus `/doctor`, `/stats`, `/debug`, a sandbox, and reliability events in the TUI

## Already done (read before starting)

- **Core:** `core/` has the gateway (`gateway/server.py`, JSON-RPC over stdio), the agent loop, and the reliability stack (`reliability/`: netwatch, persistent_retry, journal, governor, loopguard).
- **Commands:** `core/src/k3code/commands/` is a registry with one module per command.
- **Reports:**
  - M1 gateway: `docs/reports/m1-gateway.md`
  - M2 reliability: `docs/reports/m2-reliability.md`

## Build

### 1. Reliability events reach the TUI
- Forward `reliability.*` and `net.state` events from the loop to the gateway client.
- Map each event to the TUI event that displays it:

  | Loop event | TUI display |
  |---|---|
  | `paused` / `parked` | `status.update` (text `⏸ offline — will resume automatically` or `⏸ waiting for provider until HH:MM`) plus `notification.show` |
  | `resumed` | `notification.clear` |
  | `interrupted_tool` | `notification.show`, level warning |
  | `budget_exceeded` | an `error` event |
  | `loop_detected` | an `error` event |

- Mark every one of these events `importance: essential`.
- **Session state.** A session waiting on the network is `working` (paused). A session stopped by the loop guard or a budget is `needs_input`.

### 2. Daemon mode (24/7)
- Add `k3code daemon`. It runs a long-lived host process that keeps sessions alive while no TUI is attached.
  - The gateway server listens on a Unix socket, `$K3CODE_HOME/run/gateway.sock` (JSON-RPC, one connection per client), in addition to stdio.
  - The TUI attaches through `K3CODE_GATEWAY_SOCKET` (or `HERMES_TUI_GATEWAY_URL`). Prefer a small stdio↔socket bridge: `k3code gateway --attach`, used as `K3CODE_GATEWAY_CMD`. That way the TUI code needs no transport change.
  - Background sessions keep running when the TUI detaches. On attach, the client receives `session.active_list` with their states.
- Add `install/systemd/k3code.service` (user unit):
  - `Type=notify`, `NotifyAccess=main`
  - `Restart=always`, `RestartSec=5`
  - `StartLimitIntervalSec=600`, `StartLimitBurst=20`
  - `WatchdogSec=120`, with the daemon sending `WATCHDOG=1` every 30 s through `sdnotify`. Use the stdlib socket; no new dependency is needed.
  - `Nice=5`, `IOSchedulingClass=idle`
  - `MemoryHigh=2G`
- Add `k3code service install|uninstall|status`. Install writes the unit to `~/.config/systemd/user/`, runs `daemon-reload` and `enable --now`, and advises `loginctl enable-linger $USER` (print the advice only; never run sudo).
  - **Do NOT actually install or start the service on this machine in tests.** Use a `--dry-run` that prints what it would do. Integration-test the daemon by running `k3code daemon` directly in a temp `K3CODE_HOME`.
- **Restart-storm safe mode.** If the daemon restarts more than 5 times in 10 min (tracked in a state file), it starts with background work paused and emits a notification.

### 3. `/doctor` (command plus a `k3code doctor` CLI)
Each check returns `ok` / `warn` / `fail` with a fix hint. It must include:
- provider chain reachability (per entry, with latency);
- that at least one chain entry bypasses OmniRoute (`warn` if none);
- that API keys are present (never print them);
- netwatch state;
- disk space;
- PSI pressure;
- daemon/socket health;
- systemd unit status, if installed;
- TUI build present and the node version;
- `K3CODE_HOME` writable;
- the journal has no unresolved intents;
- vendor_check;
- that no Hermes `~/.hermes/.env` is being read (isolation).

Add `--json` output.

### 4. `/stats` (`k3code stats`)
- Per session and per day: tokens in/out, cost if known, calls per provider/model, failovers, retries, pauses, and time paused.
- Also: tool-call counts and the approval-prompt count.
- Persist usage in `$K3CODE_HOME/usage.db` (SQLite) from router events.

### 5. `/debug`
- `/debug` toggles verbose event logging for the session.
- `/debug dump` writes a redacted bundle (last N events, config without secrets, versions, doctor output) to `$K3CODE_HOME/debug/<ts>.tar.gz` and prints the path.

### 6. Sandbox for unattended runs
- `reliability/sandbox.py` builds a bubblewrap (`bwrap`) argv:
  - read-only `/usr`, `/etc`, `/lib*`;
  - the project dir and add-dirs read-write;
  - `$HOME` hidden except `~/.cache` and `~/.local/share/uv`;
  - network kept, because providers and tools need it;
  - `--die-with-parent`.
- When a session is in `auto` or `yolo` mode, or is a background, cron or loop session, bash tool calls run inside bwrap. If bwrap is missing, `/doctor` reports `warn` and the session falls back to running without the sandbox.
- Test that a sandboxed `bash` cannot write to `$HOME/sandbox-escape-test`.

### 7. `/model chain`
- Show the provider/model fallback chain with live cooldown/health.
- `/model chain add|remove|move` edits the user config. Run validation, then a backup of the config, then the write.

## Tests
- Event mapping: the gateway emits `status.update` on paused.
- Daemon over the Unix socket: two clients, one session, a background prompt keeps running after a client disconnects. Use the fake provider.
- Watchdog pings are sent (mock `NOTIFY_SOCKET`).
- The safe-mode counter.
- Doctor JSON shape.
- Stats aggregation.
- Debug dump redaction (no key strings in the archive).
- bwrap argv and the escape test (skip if bwrap is missing).

## Acceptance (put the outputs in REPORT.md)
- `uv run pytest -q` passes, ruff is clean, and `npm run build` succeeds.
- Run `k3code doctor --json` on this laptop and paste the summary (without secrets).
- Live: start `k3code daemon` in a temp home. Attach with `scripts/e2e_tui_file.py`, adapted to use `K3CODE_GATEWAY_CMD="… gateway --attach"`. Run a prompt, detach, and confirm `session.list` via a second attach shows it completed.
- `k3code service install --dry-run` prints the unit.
