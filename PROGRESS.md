# M2 reliability — progress

- done: `core/src/k3code/reliability/netwatch.py` (state machine, injectable probes, NM re-probe, backoff 5s→60s), `events.py` (reliability.* + net.state events), `journal.py` (fsync intent/done journal + resume plan), `governor.py` (PSI parse/admission, caps, disk guard, budgets, async slot CM). Ruff clean.
- in progress: `loopguard.py`, `persistent_retry.py`, `__init__.py`.
- next: hook points in `agent/loop.py` + `router/`, pytest tests (all modules), `scripts/chaos/` (flaky_proxy.py, offline_pause.sh, kill_during_bash.sh), full pytest + ruff, chaos runs, REPORT.md.
