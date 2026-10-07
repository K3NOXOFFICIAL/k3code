# M2-ops report

## Built
- **Reliability events → TUI** (`core/src/k3code/gateway/server.py::_on_reliability_event`). One `Reliability` bundle per session (netwatch now actually runs in gateway mode; before, the gateway never passed `reliability=` and never started it). Mapping: `paused`/`parked` → `status.update` (`⏸ offline — will resume automatically` / `⏸ waiting for provider until HH:MM`) + `notification.show`; `resumed`/`unparked` → `notification.clear`; `interrupted_tool` → `notification.show` (warning); `budget_exceeded`/`loop_detected` → `error`. Each is sent with `importance: "essential"` (a sibling of `type`/`payload` on the event frame; `tui/shared/json-rpc-channel.ts:470` passes the whole `params` object through as the event, so `focusPolicy.ts:12` sees it. Not fixed here: `focusPolicy.ts:191` reads importance off *messages*, so if the TUI turns `status.update`/`error` events into messages it does not copy the field), and the raw `reliability.*`/`net.state` event is forwarded too. Session state: `working` (also while paused) / `needs_input` (loop guard, budget) / `idle`, exposed as `state`/`status` in `session.active_list`, `session.list` rows and `live_info`.
- **Gateway refactor** (needed by the daemon): `server.live` (many sessions), per-connection `Client`, events routed to the clients attached to the session (via a contextvar for turn tasks), Unix-socket listener (`start_socket`), approvals sent to the session's clients and re-sent on attach, attach snapshot (`gateway.ready` + `session.active_list` event + safe-mode notice). stdio mode behaves as before.
- **Daemon**: `daemon.py`, `sdnotify.py`, `k3code daemon`, `k3code gateway --attach [--socket]`, `K3CODE_GATEWAY_SOCKET` / `HERMES_TUI_GATEWAY_URL` honoured. Restart-storm safe mode (`run/restarts.json`; >5 starts in 10 min → background prompts refused, notification on attach, `/daemon resume` to leave it).
- **systemd**: `install/systemd/k3code.service` (identical to `service.render_unit()` with the default ExecStart; a test enforces it), `k3code service install|uninstall|status [--dry-run]` (`service.py`; prints the `loginctl enable-linger` advice, never runs sudo). `StartLimit*` are in `[Unit]` (systemd ignores them in `[Service]`). Added `EnvironmentFile=-%h/.config/k3code/env` because a user unit does not inherit the shell's API keys.
- **/doctor + `k3code doctor [--json] [--no-probe]`** (`doctor.py`, `commands/doctor.py`). Checks: per-provider reachability+latency, OmniRoute-bypass, API keys (presence only), netwatch, disk, PSI, daemon socket, systemd unit, TUI build + node, home writable, journal, vendor_check, sandbox (bwrap), Hermes isolation. Exit 1 on any `fail`.
- **/stats + `k3code stats`**: `usage.py` (`$K3CODE_HOME/usage.db`, event table; per session/day tokens, cost, calls per provider/model, failovers, retries, pauses + paused seconds, tool calls, approvals). Cost stays NULL → "unknown" (no price table exists).
- **/debug**: toggle verbose event logging; `/debug dump [N]` → `$K3CODE_HOME/debug/<ts>.tar.gz` (`debugdump.py`: events, config, versions, doctor; every secret env value, provider key and key-shaped string redacted).
- **Sandbox**: `reliability/sandbox.py` + `tool_bash(sandbox=…)` + `AgentLoop(background=…)`. auto/yolo or background sessions run bash in bwrap; falls back unsandboxed (one warning) when bwrap is missing or unusable; `/doctor` warns.
- **/model chain** (`chain_config.py`): live view with cooldown/health; `add|remove|move` do validate → backup (`config.yaml.bak.<ts>`) → write. Also a `/daemon` command.
- Small supporting changes: `EventEmitter.add(key=)` (a per-loop sink replaces itself instead of piling up when a session builds a loop per turn), `build_reliability()` moved into `reliability/hooks.py`, `load_config` reads `K3CODE_HOME` at call time, fake provider steps accept `"when": "first"|"after_tool"`.

## Verification
- `cd core && uv run pytest -q` → 286 passed, 0 failed (includes `test_in_flight_turn_survives_client_disconnect`: the client detaches while bash is mid-`sleep`, a second client sees the session still `working` and later `idle` with the marker file written) (new: `test_ops_events.py`, `test_daemon.py`, `test_ops_tools.py`; includes the bwrap escape test, which ran because bwrap works here).
- `uv run ruff check . ../scripts` → All checks passed. `uv run python ../scripts/vendor_check.py` → All checks passed.
- `cd tui && npm ci && npm run build` → `built …/tui/dist/entry.js`.
- `K3CODE_HOME=<tmp> E2E_DAEMON=1 E2E_FAKE=1 python scripts/e2e_tui_file.py <repo> <work> <home>` (real TUI → `gateway --attach` → daemon, fake provider):
  ```
  daemon-socket-up True
  approval-prompts-answered=0
  second-attach session.list: [('252005c872ef4e5e', 'idle', 5)]
  RESULT PASS 'works'
  ```
  The TUI was killed (detach), a second raw attach listed the session as finished (`idle`, 5 messages), and the file `e2e.txt` was written. An earlier run with a looping fake script also showed `needs_input` + the `Loop detected` error in the TUI.
- `k3code doctor --json` on this laptop (no `~/.k3code/config.yaml` exists, so no providers): `{ok: 10, warn: 2, fail: 1}`; fail = "no providers configured", warns = api-keys (nothing to check), daemon not running. netwatch ok (218 ms), disk 293.9 GB, PSI 0.0/0.2 %, node v22.23.1 + TUI build present, home writable, journal clean, vendor ok, bwrap ok, Hermes isolation ok. With a temp config pointing at OmniRoute + api.anthropic.com: both providers reachable (HTTP 401 unauthenticated, 186/241 ms), bypass ok, `{ok: 13, warn: 1, fail: 1}` (fail = ANTHROPIC_API_KEY unset).
- `k3code service install --dry-run` prints the unit (see `install/systemd/k3code.service`) and the steps; the service was never installed or started here.

## Deviations / known limits
- Live e2e used the fake provider (OmniRoute quota was exhausted all day); the real-model path is unchanged by this work.
- Hermes isolation check does not read `~/.hermes/.env` (rules forbid touching it); it verifies k3code has no dotenv loader and flags `HERMES_*` env vars.
- `_ensure_router` is still server-global: switching the model key in one session rebuilds the router under others (their running turns keep the old router object). Per-session routers are a follow-up.
- `config.get full` returns provider `api_key` values to the client (pre-existing); only `/debug dump` redacts.
- Yaml rewrite in `/model chain` drops comments from `config.yaml` (the timestamped backup keeps them). It edits only the user config, not project config.
- Approvals for a detached session stay pending until a client attaches (no timeout); auto/yolo background sessions never ask.
- The daemon does not auto-resume sessions after its own restart (sessions are persisted; crash recovery is still `--resume`).
- Cost is "unknown" everywhere (no price map).

## Open TODOs
- Per-session router/cooldown stores; auto-resume of background sessions on daemon start.
- Price map for `cost_usd`; redact `api_key` in `config.get full`.
- Run the real-provider e2e once the OmniRoute quota resets.
