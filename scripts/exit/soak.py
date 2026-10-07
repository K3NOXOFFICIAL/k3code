#!/usr/bin/env python3
"""M2 soak: `k3code daemon` in a temp home on the fake provider with a /loop and 2 cron jobs.

Every --interval seconds (default 300 = 5 min) it logs: elapsed, daemon RSS, runs per source, lost turns, errors.
Verdict: PASS when lost turns == 0, no errors, the daemon is alive and RSS growth after warm-up is bounded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chaoslib as cl  # noqa: E402
from lib import REPO, emit  # noqa: E402

LOOP_EVERY = 30  # seconds
JOBS = [("soak-cron-a", "* * * * *", 60.0), ("soak-cron-b", "1m", 60.0)]
STUCK_AFTER = 300.0  # a run still 'running' after this long counts as lost
RSS_SLACK_KB = 20 * 1024  # growth allowed on top of 25 % of the warm-up RSS
ERR_RE = re.compile(r"Traceback|\bERROR\b|\bCRITICAL\b")


def sample(d: cl.Daemon, t_start: float, wall0: float) -> dict:
    """One measurement row. Runs are read from the daemon's automation.db (read-only)."""
    now = time.time()
    elapsed = now - wall0
    runs: dict[str, dict[str, int]] = {}
    lost = stuck = 0
    db = d.home / "automation.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        for owner_kind, status, n, in_flight in con.execute(
            "SELECT owner_kind, status, count(*), sum(? - started_at > ? AND finished_at IS NULL)"
            " FROM job_runs GROUP BY owner_kind, status", (now, STUCK_AFTER)):
            runs.setdefault(owner_kind, {})[status] = n
            if status in ("failed", "interrupted", "skipped", "missed"):
                lost += n
            if status == "running":
                stuck += int(in_flight or 0)
        con.close()
    lost += stuck
    # schedule shortfall: a source that should have fired floor(elapsed/period) times (-1 for boundary/in-flight)
    sources = {"loop": (LOOP_EVERY, 1), "job": (60.0, len(JOBS))}
    missing = 0
    for kind, (period, n_sources) in sources.items():
        expected = int(elapsed // period) * n_sources - n_sources
        total = sum(runs.get(kind, {}).values())
        missing += max(0, expected - total)
    log_text = d.log.read_text(errors="replace") if d.log.exists() else ""
    errors = len(ERR_RE.findall(log_text))
    done = sum(v for r in runs.values() for s, v in r.items() if s == "completed")
    return {"t": round(time.monotonic() - t_start), "elapsed_s": round(elapsed), "rss_kb": d.rss_kb(),
            "alive": d.alive(), "runs": runs, "completed": done, "lost_turns": lost + missing,
            "failed_or_stuck": lost, "missed_schedule": missing, "errors": errors}


def fmt(s: dict) -> str:
    return (f"{time.strftime('%H:%M:%S')} +{s['elapsed_s']}s rss={s['rss_kb'] / 1024:.1f}MB alive={s['alive']} "
            f"completed={s['completed']} lost_turns={s['lost_turns']} (failed/stuck={s['failed_or_stuck']}, "
            f"missed={s['missed_schedule']}) errors={s['errors']} runs={json.dumps(s['runs'], sort_keys=True)}")


def verdict(samples: list[dict], span_label: str) -> tuple[str, str]:
    last = samples[-1]
    warm = next((s for s in samples if s["elapsed_s"] >= 120), samples[0])
    growth = last["rss_kb"] - warm["rss_kb"]
    bound = warm["rss_kb"] * 0.25 + RSS_SLACK_KB
    problems = []
    if not last["alive"]:
        problems.append("daemon died")
    if last["lost_turns"]:
        problems.append(f"lost turns {last['lost_turns']}")
    if last["errors"]:
        problems.append(f"{last['errors']} errors in daemon log")
    if last["completed"] == 0:
        problems.append("no turns ran at all")
    if growth > bound:
        problems.append(f"RSS grew {growth / 1024:.1f} MB (bound {bound / 1024:.1f} MB)")
    ev = (f"{span_label}: {last['completed']} loop/cron turns completed, lost turns {last['lost_turns']}, "
          f"errors {last['errors']}, RSS {samples[0]['rss_kb'] / 1024:.1f} -> warm {warm['rss_kb'] / 1024:.1f} -> "
          f"{last['rss_kb'] / 1024:.1f} MB (growth after warm-up {growth / 1024:+.1f} MB, bound "
          f"{bound / 1024:.1f} MB)")
    return ("FAIL", ev + "; " + "; ".join(problems)) if problems else ("PASS", ev)


async def run(a: argparse.Namespace) -> int:
    total_s = a.hours * 3600 if a.hours else a.minutes * 60
    logdir = REPO / "scripts" / "exit" / "rows" / "soak"
    logdir.mkdir(parents=True, exist_ok=True)
    logf = logdir / f"soak-{time.strftime('%Y%m%d-%H%M%S')}.log"
    procs = cl.Procs()
    d = cl.Daemon(procs)
    (d.home / "fake.json").write_text(json.dumps(
        [{"type": "text", "text": "tick ok"}, {"type": "usage", "prompt_tokens": 20, "completion_tokens": 3}]))
    d.env["K3CODE_FAKE_PROVIDER"] = str(d.home / "fake.json")
    d.env["FAKE_KEY"] = "x"
    d.write_config("providers:\n  - {name: fake, kind: openai, base_url: 'http://fake', api_key_env: FAKE_KEY,"
                   " models: {default: m}}\ndefault_model: default\nheadless_permission: yolo\n"
                   "reliability: {flags: {netwatch: false}}\n")

    def log(line: str) -> None:
        print(line, flush=True)
        with open(logf, "a") as f:
            f.write(line + "\n")

    log(f"# soak start {time.strftime('%F %T')} duration={total_s}s interval={a.interval}s home={d.root}")
    samples: list[dict] = []
    try:
        d.start()
        peer = await d.peer()
        await peer.wait_for("gateway.ready", 30)
        wall0 = time.time()
        t_start = time.monotonic()
        sid = (await peer.call("session.create", cwd=str(d.proj)))["result"]["session_id"]
        await peer.call("session.mode.set", session_id=sid, mode="yolo")
        r = await peer.call("command.dispatch", name="loop", session_id=sid,
                            arg=f"{LOOP_EVERY}s --max-ticks 10000000 soak loop tick")
        log(f"# loop: {json.dumps(r.get('result') or r.get('error'))[:200]}")
        for name, expr, _ in JOBS:
            p = subprocess.run([cl.k3code_bin(), "schedule", "add", expr, "--name", name, "--cwd", str(d.proj),
                                f"{name} tick"], env=d.env, capture_output=True, text=True, timeout=60)
            log(f"# schedule add {expr!r}: {(p.stdout + p.stderr).strip()[:150]}")
        samples.append(sample(d, t_start, wall0))
        log(fmt(samples[-1]))
        while time.monotonic() - t_start < total_s and d.alive():
            await asyncio.sleep(max(0.0, min(a.interval, total_s - (time.monotonic() - t_start))))
            samples.append(sample(d, t_start, wall0))
            log(fmt(samples[-1]))
        peer.close()
    finally:
        procs.close()
    span = f"{a.hours} h" if a.hours else f"{a.minutes:g} min"
    status, ev = verdict(samples, f"{span} soak")
    log(f"# verdict {status}: {ev}")
    (logdir / (logf.stem + ".verdict.json")).write_text(json.dumps({"status": status, "evidence": ev}))
    if a.report:
        how = (f"scripts/exit/soak.sh --{'hours' if a.hours else 'minutes'} {a.hours or a.minutes:g}: daemon in a "
               f"temp home on the fake provider with /loop {LOOP_EVERY}s + 2 cron jobs (every minute); sampled every "
               f"{a.interval:g}s; log {logf.relative_to(REPO)}")
        if a.hours:
            emit("M2", f"{a.hours:g} h soak: no lost turns, no errors, bounded memory", how, status,
                 ev + f"\nlog: {logf.relative_to(REPO)}")
        else:
            emit("M2", f"(f) soak ({span}): no lost turns, no errors, bounded memory growth", how, status,
                 ev + f"\nlog: {logf.relative_to(REPO)}")
            emit("M2", "(f) 72 h soak: no lost turns, no errors, bounded memory", "same harness, --hours 72",
                 "PENDING", "not run: needs 72 h of wall-clock; the 30-min run above is the same harness",
                 close="nohup scripts/exit/soak.sh --hours 72 --report &   # then read scripts/exit/rows/soak/*.log")
    if not a.hours:
        log("continue for 72 h:  nohup scripts/exit/soak.sh --hours 72 &")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--minutes", type=float, default=30)
    g.add_argument("--hours", type=float)
    ap.add_argument("--interval", type=float, default=300.0, help="seconds between samples (default 300 = 5 min)")
    ap.add_argument("--report", action="store_true", help="emit the M2 soak row(s) to $EXIT_ROWS")
    sys.exit(asyncio.run(run(ap.parse_args())))
