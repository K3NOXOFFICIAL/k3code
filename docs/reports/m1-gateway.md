# M1-gateway — completion report (2026-10-07)

Core speaks the TUI gateway protocol: JSON-RPC 2.0 stdio server, slash
commands, CLI `gateway --stdio` + TUI launch, router network cooldown,
fake-provider test hook, pytest suite, rebuilt TUI, live pty e2e.

## Verification (all run from the `w/m1-gateway` worktree)

| Check | Command | Result |
|---|---|---|
| pytest (67: 41 M0 + 26 M1) | `cd core && uv run pytest` | **PASS** — 67 passed in ~3s |
| ruff lint | `cd core && uv run ruff check src tests ../scripts/e2e_tui.py` | **PASS** — All checks passed |
| ruff format (touched files) | `ruff format --check src/k3code/providers/fake.py src/k3code/providers/__init__.py tests/test_gateway.py ../scripts/e2e_tui.py` | **PASS** — 4 files already formatted |
| gateway ready frame | `printf '' \| uv run python -m k3code.cli gateway --stdio` | **PASS** — `gateway.ready` with `skin k3code`, `replay_epoch 1` |
| unknown method → -32601 | `printf '{"jsonrpc":"2.0","id":1,…}' \| … gateway --stdio` | **PASS** — `-32601 Method not found: bogus.x` |
| TUI build | `cd tui && npm run build` | **PASS** — `dist/entry.js` 3.6–3.8 MB |
| TUI typecheck | `cd tui && npm run typecheck` | **PASS** — exit 0, no errors |
| **live e2e (pty TUI → gateway → OmniRoute)** | `cd core && uv run python ../scripts/e2e_tui.py --timeout 180` | **PASS** — boot marker seen, `E2E-OK` reply rendered on screen |

Live e2e detail: `scripts/e2e_tui.py` spawns `node tui/dist/entry.js` in a
40×140 pty with `K3CODE_GATEWAY_CMD="<venv python> -m k3code.cli gateway
--stdio"`, `K3CODE_HOME=/tmp/k3smoke` (OmniRoute provider config,
`auto/coding-cheap`), sends "Reply with exactly: E2E-OK", and asserts the
reply text renders. Uses the real `OMNIROUTE_API_KEY` from the environment.

## What was built (branch `w/m1-gateway`, 6 commits)

- `6079990` — `core/src/k3code/gateway/` package: asyncio JSON-RPC 2.0
  stdio server, all **33 M1-core methods**, event emission, approval
  request/response, SQLite sessions at `$K3CODE_HOME/sessions.db`.
  All logging to stderr; `-p` stdout stays the final answer.
- `11509ce` — `core/src/k3code/commands/` slash registry
  (`/model /effort /clear /compact /rename /resume /stop /exit /help`);
  agent-loop gateway hooks; network failures → **60 s cooldown**,
  tunable via `K3CODE_NETWORK_COOLDOWN_SECONDS` (0 disables).
- `79963fe` — `cli.py`: `gateway --stdio` subcommand; no-args + tty →
  launches TUI with `K3CODE_GATEWAY_CMD`; `--repl` for the old REPL.
- `34874c9` — `tui/src/gatewayClient.ts` honors `K3CODE_GATEWAY_CMD`
  (safe argv split, no shell).
- *(uncommitted at report time → commit below)* — `providers/fake.py`
  (`K3CODE_FAKE_PROVIDER=<script.json>` scripted offline provider),
  `core/tests/test_gateway.py` (26 tests: framing, approval round-trip,
  dispatch, fake, cooldown), `pexpect` dev dep, `scripts/e2e_tui.py`.

## Deviations from the task spec

- `K3CODE_FAKE_PROVIDER` replaces **one FakeProvider per configured
  entry** (not a single global fake) so chain failover still exercises.
- `session.create` etc. persist via `SessionStore`; `events.since` is an
  empty-but-correct-shape stub (no replay buffer in M1).
- `image/file/pdf.attach` return `-32602` (text-only providers in M1).
- `ruff format --check` on the whole tree flags 5 pre-existing M0 files;
  left untouched, only M1-touched files verified formatted.
- Live e2e used `https://<omniroute-public-host>/v1` (the `/tmp/k3smoke` smoke
  config) rather than `http://<omniroute-host>:20128/v1` from the spec —
  same OmniRoute service, reachable endpoint.

## Open TODOs

- `events.since` replay buffer (M2 or later milestone).
- Image/file/PDF attachment support (M1 is text-only by design).
- Persisting new provider keys via `model.save_key` (M1: env-var keys only).
- `PROGRESS.md` milestone log (no `PROGRESS.md` exists in this repo yet).

## Backup

Pre-verification snapshot of all uncommitted work:
`/tmp/k3code-m1-backup-20261007/` (`working.patch` + `fake.py` + `test_gateway.py`).
