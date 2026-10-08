# M2-reliability: offline pause and resume, persistent retry, a crash-safe journal, and a resource governor

`core/` is the k3code Python core (`cd core && uv run pytest`). It already has these pieces:
- `router/`: the provider chain with retries, cooldown, and `AllProvidersUnreachable`.
- `agent/loop.py`: the agent loop.
- `tools/`: each tool declares `side_effect: bool`.

Another worker is building `gateway/` and `commands/` in parallel. Do not edit those directories. Keep your changes to new modules plus small, well-marked hook points in `agent/loop.py` and `router/`.

## Build (new modules in `core/src/k3code/reliability/`)

1. **`netwatch.py`: connectivity state machine**
   - **States:**
     - `ONLINE`
     - `DEGRADED` (slow or partial)
     - `PROVIDER_DOWN` (the internet works but a provider endpoint does not)
     - `CAPTIVE` (an HTTP probe gets redirected or receives unexpected content)
     - `OFFLINE`
   - **Signals:**
     - NetworkManager over D-Bus, if available. Use `dbus-next` or a subprocess call to `nmcli -t -f STATE general`, and degrade gracefully when neither is present.
     - A per-endpoint HTTP probe (HEAD/GET with a short timeout) for each provider base URL.
     - A generic internet probe, configurable, defaulting to `https://www.gstatic.com/generate_204` and `1.1.1.1:443` over TCP.
   - **API:**
     - an async `NetWatch` with `state`, `subscribe(callback)` and `wait_until_usable(provider)`;
     - polling with backoff while bad: 5 s, rising to 60 s;
     - an immediate re-probe when NetworkManager reports a change.
   - PROVIDER_DOWN for one provider must NOT count as OFFLINE. It means "fail over", not "pause".
2. **`persistent_retry.py`: never give up**
   - Wrap the router call that the agent loop makes.
   - **On `AllProvidersUnreachable`:**
     - If netwatch reports `OFFLINE` or `CAPTIVE`: emit `paused(reason)`, wait until online, emit `resumed`, then retry the same turn without losing messages.
     - If it reports `PROVIDER_DOWN` for every provider in the chain: park with exponential backoff (30 s up to 10 min), emitting `parked(next_retry_at)`.
   - **On rate limit or quota** (`Retry-After` or a reset time): park until then.
   - There is no maximum retry count by default (`max_wait` can be configured). A cancel token from `/stop` aborts.
3. **`journal.py`: crash-safe tool journal**
   - Before a tool runs, append an `intent` record to `$K3CODE_HOME/journal/<session>.jsonl`: tool, args hash, `side_effect`, and timestamp. After it runs, append `done` with a result digest.
   - **On resume after a crash, for every `intent` without a `done`:**
     - Pure tools (`side_effect=False`) may be re-run.
     - Side-effect tools must NOT be re-run. Synthesize a tool result: `INTERRUPTED: <tool> may or may not have completed before a crash; inspect state (e.g. git diff / file contents) before retrying.`
   - Use fsync on the intent records.
4. **`governor.py`: resource governor**
   - Admission control for concurrent agents and background jobs:
     - read Linux PSI from `/proc/pressure/io` and `/proc/pressure/cpu`, using `some avg10`;
     - admit IO-heavy work only when IO `some avg10` is below a threshold (default 20);
     - hard cap: at most 2 IO-heavy and 4 total concurrent workers, configurable;
     - a per-provider concurrent-stream cap (default 4).
   - A disk guard refuses to start new work when free space on `$K3CODE_HOME` is below 2 GB.
   - Budgets: tokens or USD per session, per day and per job. Read usage from router events and stop with a clear message when a budget is exceeded.
   - The API is an async context manager: `async with governor.slot(kind="io"|"cpu"|"llm", provider=...)`.
5. **Doom-loop guard** (`reliability/loopguard.py`)
   - Detect the same tool and the same args (normalized) called 3 or more times in a row, or an identical assistant message repeated.
   - Inject a corrective system note once. On a second trigger, stop the turn and mark the session `needs_input`.
6. **Hooks.**
   - Integrate 2–5 into `agent/loop.py`, each behind a config flag and on by default.
   - Emit events through the existing event callback so the gateway can forward them later: `reliability.paused`, `reliability.resumed`, `reliability.parked`, `reliability.interrupted_tool`, `reliability.budget_exceeded`, `reliability.loop_detected`, `net.state`.

## Tests (pytest, no real network)
- netwatch state transitions using fake probes, covering a flapping network and the PROVIDER_DOWN vs OFFLINE distinction.
- persistent_retry:
  - Offline, then online: the turn completes, and `paused`/`resumed` are emitted.
  - All providers down: the call parks and then succeeds when they recover. Use a fake clock or very small delays.
  - `/stop` cancels a parked wait.
- journal:
  - A simulated crash after the `intent` of a `bash` call: resume synthesizes INTERRUPTED and does not run the tool again.
  - A pure tool is re-run.
- governor: the PSI parser (from fixture text), the caps, the disk guard and budget exhaustion.
- loopguard triggers.

## Chaos scripts (`scripts/chaos/`, runnable by hand; outputs go in REPORT.md where possible)
- `offline_pause.sh` runs `k3code -p` with a task that takes several turns. Midway it makes the provider unreachable without root, by pointing the config at a local TCP proxy (`scripts/chaos/flaky_proxy.py`) that it can switch between up and down. It shows `paused` and then `resumed`, and the task finishing. **Do not** run `nmcli networking off` on this laptop, because it would cut off the other workers.
- `kill_during_bash.sh` starts a task whose bash tool sleeps, `kill -9`s the process mid-tool, resumes, and checks that INTERRUPTED appears and the bash command was not re-run (a marker file is written only once).

## Acceptance
- `cd core && uv run pytest -q` passes (old and new tests), and ruff is clean.
- Both chaos scripts pass when run against the flaky proxy plus OmniRoute (`OMNIROUTE_API_KEY` is in the env; use `base_url http://<omniroute-host>:20128/v1` as the upstream behind the proxy).
