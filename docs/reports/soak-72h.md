# The 72-hour soak (M2 exit criterion (f))

**Status: RUNNING.** Started **2026-10-08 02:02 (Europe/Berlin)** as the systemd user unit `k3code-soak-72h.service` (own cgroup; lingering is on, so it survives closing the terminal and logging out). It ends about **2026-10-11 02:02**. Earlier starts were stopped and restarted each time the frozen code changed.

What it runs: a `k3code daemon` in a throwaway home on the scripted fake provider, with a `/loop 30s` in one session
and two cron jobs (every minute), sampled every 5 minutes: runs completed, lost turns (failed/stuck/skipped ticks),
errors in the daemon log, daemon RSS. Compaction is on (`context.compact_at_tokens: 4000`), so the RSS bound measures
leaks and not the growth of one endless conversation. Verdict: PASS when nothing was lost, there were no errors, the
daemon stayed alive and RSS growth after warm-up stays below 25 % of the warm RSS + 20 MB.

## Where things are

| What | Path |
|---|---|
| frozen code (detached worktree at commit `8774f0d`, own venv) | `~/src/k3code/.k3dev/soak/src` (**do not touch**: any change to the daemon code restarts the 72 h) |
| sample log (one line per 5 min) | `~/src/k3code/.k3dev/soak/src/scripts/exit/rows/soak/soak-20261008-020206.log` |
| stdout of the run | `~/src/k3code/.k3dev/soak/soak-72h.out` |
| the exit row, written when it ends | `~/src/k3code/.k3dev/soak/rows-72h.jsonl` |
| the unit | `systemctl --user status k3code-soak-72h` (the daemon's home is `/tmp/k3exit.*`) |

```
tail -n 3 ~/src/k3code/.k3dev/soak/src/scripts/exit/rows/soak/soak-20261008-020206.log
cat ~/src/k3code/.k3dev/soak/rows-72h.jsonl          # when it has finished: status + evidence
```

## Things that make it fail for reasons that are not bugs

- **The laptop suspends or loses power.** The run holds a `systemd-inhibit --what=sleep:idle` lock, which stops
  *idle* suspend but not a closed lid or a dead battery; keep the machine on AC power with the lid open (or set
  `HandleLidSwitch=ignore`). A suspend shows up as skipped loop ticks.
- A reboot ends it (the daemon is a child of the run, not a service).
- To stop it early: `systemctl --user stop k3code-soak-72h` (stops the driver and the daemon; a stopped run writes no verdict and no exit row).

## If it passes

Merge the row into the report: append `rows-72h.jsonl` to `scripts/exit/rows/rows.jsonl` (replacing the M2 "72 h soak"
PENDING row) and run `python3 scripts/exit/render.py scripts/exit/rows/rows.jsonl docs/reports/exit-status.md`.
