#!/usr/bin/env bash
# M2 soak: daemon on the fake provider with a /loop and 2 cron jobs, sampled every 5 min.
#   soak.sh --minutes N [--interval S] [--report]     (run_all.sh uses --minutes 30 --report)
#   soak.sh --hours H   [--interval S] [--report]     (the 72 h run: nohup scripts/exit/soak.sh --hours 72 --report &)
# Log: scripts/exit/rows/soak/soak-<start>.log. With --report the verdict is emitted as M2 rows via $EXIT_ROWS.
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/soak.py" "$@"
