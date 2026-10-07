# PROGRESS (M2-ops)

## Done
- Step 1: reliability events → TUI (`gateway/server.py::_on_reliability_event`), per-session `Reliability`, session state (`working`/`needs_input`/`idle`), `usage.py` (UsageDB).
- Gateway refactor: multi-session (`server.live`), multi-client (`Client`, Unix socket via `start_socket`).

## In progress / next (in order)
- daemon (`daemon.py`, sdnotify, safe mode, `gateway --attach`, systemd unit, `service` cmd) + tests
- doctor, stats, debug, sandbox, `/model chain`
- REPORT.md (acceptance outputs)
