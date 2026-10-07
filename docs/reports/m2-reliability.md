# M2-reliability report

## Built
- `core/src/k3code/reliability/`: `netwatch.py` (5-state machine, nmcli + HTTP/TCP probes, 5→60 s backoff, NM-change re-probe), `persistent_retry.py` (pause/park/resume, Retry-After, `/stop` cancel, optional `max_wait`), `journal.py` (fsync intent/done, resume plan), `governor.py` (PSI, caps, disk guard, budgets, `slot()` CM), `loopguard.py`, `events.py`, `hooks.py` (`Reliability` bundle, flags default on).
- Hooks in `agent/loop.py` (marked `M2:`) and `cli.py` (headless + REPL). New `--session ID` / `--resume` CLI options. The loop persists the transcript to `$K3CODE_HOME/journal/<session>.messages.json`. On `--resume`, open tool calls are completed: side-effect tools with an intent but no `done` get a synthesized `INTERRUPTED` result and are never re-run; pure or never-started calls run normally.
- Config: `reliability.netwatch: {...}` overrides NetWatchConfig (used by chaos scripts to aim probes at the proxy).
- Fixes made this session: provider probe no longer builds `/v1/v1/models`; generic probe treats any HTTP status < 500 as reachable (401 is not "offline").
- `scripts/chaos/`: `flaky_proxy.py` (mode file `up`/`down`; down = stop listening), `fake_upstream.py`, `_common.sh`, `offline_pause.sh`, `kill_during_bash.sh`.
- Tests: netwatch, persistent_retry, journal, governor, loopguard, plus `test_resume_transcript.py`.

## Verification
- `cd core && uv run pytest -q` → all passed (105 tests); `uv run ruff check . ../scripts` → clean.
- `scripts/chaos/offline_pause.sh` → PASS: `net.state online -> offline`, `reliability.paused`, `net.state offline -> online`, `reliability.resumed`, task finished (c.txt written).
- `scripts/chaos/kill_during_bash.sh` → PASS: kill -9 mid-bash (1 intent, 0 done); resume emitted `reliability.interrupted_tool`, INTERRUPTED is in the transcript, marker file holds `run` once and never `finished`.

## Deviations
- **The chaos scripts ran against `fake_upstream.py`, not OmniRoute.** The OmniRoute API key hit its daily quota (HTTP 429 on every model, "resets in ~18h"), so a real LLM run was impossible. `_common.sh` probes OmniRoute and falls back to the fake automatically (`CHAOS_UPSTREAM=real|fake` forces one). Re-run with the real upstream after the quota resets. The 429 itself showed parking on Retry-After (log: "retry ... in 64877s (rate_limit)").
- Provider probes are unauthenticated; a 401 counts as "endpoint reachable".

## Open TODOs
- Re-run both chaos scripts against real OmniRoute once quota resets.
- Gateway (`reliability.*` events → JSON-RPC) is for the other worker.
- The REPL does not auto-resume sessions; only headless `--resume`.
