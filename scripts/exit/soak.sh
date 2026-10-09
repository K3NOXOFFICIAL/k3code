#!/usr/bin/env bash
# M2 soak: daemon on the fake provider with a /loop and 2 cron jobs, sampled every 5 min.
#   soak.sh --minutes N [--interval S] [--report]     (run_all.sh uses --minutes 30 --report)
#   soak.sh --hours H   [--interval S] [--report]     (the 72 h run: nohup scripts/exit/soak.sh --hours 72 --report &)
# Log: scripts/exit/rows/soak/soak-<start>.log. With --report the verdict is emitted as M2 rows via $EXIT_ROWS.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The harness imports k3code (gateway.auth for the daemon's privileged RPCs): run it with the project's venv,
# not the system python3, which has no k3code.
PY="$HERE/../../core/.venv/bin/python"
if [ ! -x "$PY" ]; then exec uv run --project "$HERE/../../core" python "$HERE/soak.py" "$@"; fi
exec "$PY" "$HERE/soak.py" "$@"
