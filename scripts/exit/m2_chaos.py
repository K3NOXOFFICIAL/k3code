#!/usr/bin/env python3
"""M2 exit criteria (a)-(e): chaos through the flaky proxy against a fake upstream. Ports 18100-18199.

(f) the soak lives in soak.sh. Rows are emitted via lib.emit(); a row is PASS only when its assertions hold.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chaoslib as cl  # noqa: E402
from lib import REPO, emit  # noqa: E402

BASE = int(os.environ.get("M2_PORT_BASE", "18100"))
NETWATCH = """reliability:
  netwatch:
    http_probe_url: http://127.0.0.1:{web}/v1/models
    tcp_probe_host: 127.0.0.1
    tcp_probe_port: {web}
    base_interval: 1.0
    max_interval: 3.0
    provider_fail_threshold: 1
"""


def provider(name: str, port: int, key_env: str) -> str:
    return (f"  - {{name: {name}, kind: openai, base_url: 'http://127.0.0.1:{port}/v1', api_key_env: {key_env},"
            f" models: {{default: m}}}}\n")


def config(providers: str, web: int, extra: str = "") -> str:
    return ("providers:\n" + providers + "default_model: default\nmax_turns: 12\nheadless_permission: yolo\n"
            + extra + NETWATCH.format(web=web))


async def start(procs: cl.Procs, cfg: str, env: dict | None = None) -> tuple[cl.Daemon, cl.Peer, str]:
    d = cl.Daemon(procs, extra_env=env)
    d.write_config(cfg)
    d.start()
    peer = await d.peer()
    await peer.wait_for("gateway.ready", 20)
    sid = (await peer.call("session.create", cwd=str(d.proj)))["result"]["session_id"]
    await peer.call("session.mode.set", session_id=sid, mode="yolo")
    return d, peer, sid


def ev_lines(peer: cl.Peer, t0: float, kinds: tuple[str, ...]) -> str:
    out = []
    for ts, e in peer.events:
        t = e.get("type", "")
        if t in kinds:
            p = e.get("payload") or {}
            out.append(f"+{ts - t0:5.1f}s {t} {str(p.get('text') or p.get('detail') or '')[:90]}")
    return "\n".join(out)


async def cmd(peer: cl.Peer, sid: str, name: str, arg: str = "") -> str:
    r = await peer.call("command.dispatch", name=name, arg=arg, session_id=sid)
    res = r.get("result") or r.get("error") or {}
    return str(res.get("message") or res.get("output") or res)


KINDS = ("reliability.paused", "reliability.resumed", "reliability.parked", "reliability.unparked", "net.state",
         "status.update", "message.complete", "error")


async def check_a(procs: cl.Procs) -> None:
    web, up, px = BASE + 10, BASE + 11, BASE + 12
    crit = "(a) an invalid primary key fails over to the secondary within 30 s, and /stats shows the failover"
    how = ("daemon in temp home, 2 providers via flaky proxy -> fake upstream that only accepts the secondary's key; "
           "primary key invalid (401); prompt via gateway, then /stats")
    mode = Path(tempfile.mkdtemp(prefix="k3exit.m")) / "mode"
    log = mode.parent / "upstream.log"
    cl.start_fake_upstream(procs, up, "--good-key", "good-key-secondary", "--log", str(log))
    cl.start_fake_upstream(procs, web)
    cl.start_proxy(procs, px, up, mode)
    d, peer, sid = await start(
        procs, config(provider("primary", px, "PRIMARY_KEY") + provider("secondary", px, "SECONDARY_KEY"), web),
        env={"PRIMARY_KEY": "bad-key-primary", "SECONDARY_KEY": "good-key-secondary"})
    t0 = time.monotonic()
    await peer.call("prompt.submit", session_id=sid, text="say hi")
    done = await peer.wait_for("message.complete", 40, after=t0)
    dt = (time.monotonic() - t0) if done else float("nan")
    stats = await cmd(peer, sid, "stats", "session")
    reqs = log.read_text() if log.exists() else ""
    ok = done is not None and dt < 30 and "failovers 1" in stats and " 401 " in reqs and " 200 " in reqs
    emit("M2", crit, how, "PASS" if ok else "FAIL",
         f"answered in {dt:.1f}s after failover\n{ev_lines(peer, t0, ('status.update', 'message.complete'))}\n"
         f"upstream status log (ts status key): {' | '.join(reqs.split(chr(10))[:3])}\n{stats}")
    peer.close()


async def check_b(procs: cl.Procs) -> None:
    web, up, px = BASE + 20, BASE + 21, BASE + 22
    crit = "(b) proxy down mid-goal gives paused, proxy up resumes within 10 s"
    how = ("daemon + flaky proxy (mode file down/up) -> fake upstream; 4-step bash task (the multi-step goal) with "
           "the proxy killed after step 1 and restored 3 s after 'paused'; resume latency = proxy up -> "
           "reliability.resumed event and next tool progress")
    mode = Path(tempfile.mkdtemp(prefix="k3exit.m")) / "mode"
    cl.start_fake_upstream(procs, up)
    cl.start_fake_upstream(procs, web)
    cl.start_proxy(procs, px, up, mode)
    # Real offline: the connectivity probe goes through the same proxy, so it goes down together with the
    # provider (as `nmcli networking off` would). A provider down while the probe stays up is PROVIDER_DOWN
    # (failover/park, never a pause); that case is check (e).
    d, peer, sid = await start(procs, config(provider("omni", px, "K"), px), env={"K": "x"})
    prompt = ("Do these steps in order, one bash tool call each, then reply DONE: RUN[echo one > a.txt] "
              "RUN[sleep 2; echo two > b.txt] RUN[echo three > c.txt] RUN[cat a.txt b.txt c.txt]")
    t0 = time.monotonic()
    await peer.call("prompt.submit", session_id=sid, text=prompt)
    for _ in range(120):
        if (d.proj / "a.txt").exists():
            break
        await asyncio.sleep(0.25)
    mode.write_text("down")
    t_down = time.monotonic()
    paused = await peer.wait_for("reliability.paused", 60, after=t_down)
    await asyncio.sleep(3)
    mode.write_text("up")
    t_up = time.monotonic()
    resumed = await peer.wait_for("reliability.resumed", 60, after=t_up)
    done = await peer.wait_for("message.complete", 90, after=t_up)
    lat = (resumed[0] - t_up) if resumed else float("nan")
    fin = (done[0] - t_up) if done else float("nan")
    ok = paused is not None and resumed is not None and lat <= 10 and done is not None and (d.proj / "c.txt").exists()
    emit("M2", crit, how, "PASS" if ok else "FAIL",
         f"paused {paused[0] - t_down:.1f}s after proxy down; resumed {lat:.1f}s after proxy up; task finished "
         f"{fin:.1f}s after proxy up; c.txt={(d.proj / 'c.txt').exists()}\n{ev_lines(peer, t0, KINDS)}")
    peer.close()


def check_c() -> None:
    crit = "(c) kill -9 during bash gives INTERRUPTED and no re-run"
    how = ("scripts/chaos/kill_during_bash.sh against the fake upstream through the proxy (headless k3code -p, "
           "kill -9 mid `sleep 20`, then --resume): asserts interrupted_tool event, INTERRUPTED in transcript, "
           "marker run exactly once")
    env = dict(os.environ, CHAOS_UPSTREAM="fake", PROXY_PORT=str(BASE + 30), OMNIROUTE_API_KEY="x")
    p = subprocess.run(["bash", str(REPO / "scripts/chaos/kill_during_bash.sh")], env=env, capture_output=True,
                       text=True, timeout=300)
    out = p.stdout + p.stderr
    emit("M2", crit, how, "PASS" if p.returncode == 0 and "PASS kill_during_bash" in out else "FAIL", out)


async def check_d(procs: cl.Procs) -> None:
    web, up, px = BASE + 40, BASE + 41, BASE + 42
    crit = "(d) a 429 with Retry-After 120 parks then resumes"
    how = ("fake upstream with a 5 s Retry-After (not 120 s, for speed; router.max_inline_wait lowered to 2 s so the "
           "5 s window is parked on, as a 120 s one would be) via proxy; single provider")
    mode = Path(tempfile.mkdtemp(prefix="k3exit.m")) / "mode"
    log = mode.parent / "upstream.log"
    cl.start_fake_upstream(procs, up, "--ratelimit", "5", "--log", str(log))
    cl.start_fake_upstream(procs, web)
    cl.start_proxy(procs, px, up, mode)
    d, peer, sid = await start(procs, config(provider("omni", px, "K"), web, "router: {max_inline_wait: 2}\n"),
                               env={"K": "x"})
    t0 = time.monotonic()
    await peer.call("prompt.submit", session_id=sid, text="say hi")
    parked = await peer.wait_for("reliability.parked", 30, after=t0)
    unparked = await peer.wait_for("reliability.unparked", 40, after=t0)
    done = await peer.wait_for("message.complete", 40, after=t0)
    park_s = (unparked[0] - parked[0]) if parked and unparked else float("nan")
    reqs = log.read_text().split("\n") if log.exists() else []
    ok = (parked is not None and unparked is not None and done is not None and 3 <= park_s <= 10
          and any(" 429 " in r for r in reqs) and any(" 200 " in r for r in reqs))
    emit("M2", crit, how, "PASS" if ok else "FAIL",
         f"parked for {park_s:.1f}s (Retry-After 5 s), then the turn completed\n"
         f"{ev_lines(peer, t0, KINDS)}\nupstream (ts status): {' | '.join(r[:20] for r in reqs if r)}")
    peer.close()


async def check_e(procs: cl.Procs) -> None:
    web, up, px = BASE + 50, BASE + 51, BASE + 52
    crit = "(e) a provider down while the internet is up gives PROVIDER_DOWN with failover, never paused"
    how = ("2 providers: primary via the flaky proxy (down = connection refused), secondary direct to the fake "
           "upstream; the internet probe points at a separate always-up local server; prompt via gateway; "
           "asserts failover to the secondary, no reliability.paused, primary reported provider_down by /model chain")
    mode = Path(tempfile.mkdtemp(prefix="k3exit.m")) / "mode"
    cl.start_fake_upstream(procs, up)
    cl.start_fake_upstream(procs, web)
    cl.start_proxy(procs, px, up, mode)
    d, peer, sid = await start(
        procs, config(provider("primary", px, "K") + provider("secondary", up, "K"), web), env={"K": "x"})
    mode.write_text("down")
    await asyncio.sleep(4)  # let netwatch probe the provider endpoints
    t0 = time.monotonic()
    await peer.call("prompt.submit", session_id=sid, text="say hi")
    done = await peer.wait_for("message.complete", 40, after=t0)
    await asyncio.sleep(1)
    chain = await cmd(peer, sid, "model", "chain")
    stats = await cmd(peer, sid, "stats", "session")
    failover = any("failover:" in str((e.get("payload") or {}).get("text", "")) for _, e in peer.events)
    paused = peer.count("reliability.paused")
    ok = done is not None and failover and paused == 0 and "provider_down" in chain.lower().replace(" ", "_")
    emit("M2", crit, how, "PASS" if ok else "FAIL",
         f"paused events: {paused}; failover seen: {failover}\n{ev_lines(peer, t0, KINDS)}\n{chain}\n{stats}")
    peer.close()


async def main() -> None:
    procs = cl.Procs()
    try:
        for fn in (check_a, check_b, check_d, check_e):
            try:
                await fn(procs)
            except Exception as e:  # noqa: BLE001
                emit("M2", fn.__name__, "m2_chaos.py", "FAIL", f"check crashed: {type(e).__name__}: {e}")
        # (c) is a subprocess script; keep emit order a-e by sorting at render time
        check_c()
    finally:
        procs.close()


if __name__ == "__main__":
    asyncio.run(main())
