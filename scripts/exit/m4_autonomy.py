#!/usr/bin/env python3
"""M4 exit checks (autonomy). Fake provider for harness behaviour; live OmniRoute only for model quality.

Run: scripts/exit/m4_autonomy.py  (re-execs under `uv run --project core` when k3code is not importable).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CORE = REPO / "core"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CORE / "tests"))
sys.path.insert(0, str(CORE / "src"))

try:
    import k3code  # noqa: F401
    import mcp  # noqa: F401
except ImportError:
    os.execvp("uv", ["uv", "run", "--project", str(CORE), "python", __file__, *sys.argv[1:]])

import lib  # noqa: E402
from lib import emit, tail  # noqa: E402

M = "M4"
TMP = Path(tempfile.mkdtemp(prefix="exit-m4-"))
os.environ["K3CODE_HOME"] = str(TMP / "home")  # never touch the real home
os.environ.pop("K3CODE_FAKE_PROVIDER", None)
LIVE_OK, LIVE_DETAIL = lib.omniroute_quota()  # one probe call, shared by every live row
RESET = "after the OmniRoute daily quota resets (2026-10-08T03:00Z per the 429 response)"


def safe(name: str, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        emit(M, name, "check crashed", "FAIL", f"{type(e).__name__}: {e}")


class MP:
    def setenv(self, k: str, v: str) -> None:
        os.environ[k] = v


def sub(name: str) -> Path:
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- 1. scope eval
def scope_eval() -> None:
    crit = "30-task scope eval: classifier agrees with labels on >=80%"
    rows = [json.loads(x) for x in (HERE / "scope_eval.jsonl").read_text().splitlines() if x.strip()]
    from k3code.autonomy.scope import SCOPES, apply_floor, classifier_messages, parse_verdict, repo_summary

    assert len(rows) == 30 and all(r["scope"] in SCOPES and r["label_source"] == "proposed-by-claude" for r in rows)
    dist = {s: sum(r["scope"] == s for r in rows) for s in SCOPES}
    how = "scripts/exit/scope_eval.jsonl (30 labels, proposed-by-claude) through the product's scope classifier prompt"
    if not LIVE_OK:
        emit(M, crit, how, "PENDING",
             f"eval set written and validated (30 rows, label dist {dist}); live classifier not run: {LIVE_DETAIL}",
             f"the owner confirms/edits the labels in scope_eval.jsonl, then re-run scripts/exit/m4_autonomy.py {RESET}")
        return
    key = os.environ["OMNIROUTE_API_KEY"]
    summary = repo_summary(REPO)
    hit, misses = 0, []
    for r in rows:
        msgs = classifier_messages(r["prompt"], summary, "")
        body = {"model": "auto/coding-cheap", "max_tokens": 400,
                "messages": [{"role": m.role, "content": m.content} for m in msgs]}
        req = urllib.request.Request("http://<omniroute-host>:20128/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {key}", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = json.load(resp)["choices"][0]["message"]["content"]
        v = parse_verdict(text)
        v = apply_floor(v, r["prompt"]) if v else None
        if v and v.scope == r["scope"]:
            hit += 1
        else:
            misses.append(f"#{r['id']} want {r['scope']} got {v.scope if v else 'unparsed'}")
    pct = 100 * hit / len(rows)
    emit(M, crit, how + " (live, auto/coding-cheap)", "PASS" if pct >= 80 else "FAIL",
         f"agreement {hit}/30 = {pct:.0f}% (target >=80%); labels are proposed-by-claude, the owner must confirm. "
         f"misses: {'; '.join(misses)}")


# ---------------------------------------------------------------- 2. HUGE fan-out
def fanout() -> None:
    crit = "HUGE task fan-out: workers merge and pass (fake demo_ultracode.py)"
    rc, out = lib.run(["uv", "run", "python", "scripts/demo_ultracode.py"], cwd=CORE, timeout=300)
    m = re.search(r"fanout\.done\s+merged (\d+)/(\d+), tests (\w+)", out)
    repo = re.search(r"^repo: (\S+)", out, re.M)
    files = ""
    if repo:
        files = ",".join(sorted(p.name for p in Path(repo.group(1)).glob("f*.txt")))
    ok = bool(m) and rc == 0 and m.group(1) == m.group(2) and int(m.group(1)) >= 3 and m.group(3) == "pass"
    ok = ok and "Final tests\npass" in out and {"f1.txt", "f2.txt", "f3.txt"} <= set(files.split(","))
    ev = f"rc={rc}; {m.group(0) if m else 'no fanout.done line'}; files in merged repo: {files}\n" + tail(
        "\n".join(ln for ln in out.splitlines() if "merged" in ln or "Final tests" in ln or "Budget" in ln), 4)
    emit(M, crit, "cd core && uv run python scripts/demo_ultracode.py (fake provider, 3 parallel worktree workers)",
         "PASS" if ok else "FAIL", ev)


# ---------------------------------------------------------------- 3. /stats tiers
def stats_tiers() -> None:
    crit = "/stats shows background, cron and loop turns on cheap tiers"
    from k3code.automation.clock import FakeClock
    from k3code.automation.engine import AutomationEngine
    from m1cmd_helpers import cmd, new_session
    from test_autonomy_gateway import make

    async def noop() -> None:
        return None

    async def until(cond, tries: int = 600) -> None:
        for _ in range(tries):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition never became true")

    async def go() -> str:
        d = sub("stats")
        server = make(d, MP(), [{"type": "text", "text": "ok"}, {"type": "usage", "prompt_tokens": 100,
                                                                   "completion_tokens": 10}], mode="auto")
        clock = FakeClock()
        eng = AutomationEngine(server, clock=clock, wait_online=noop)
        server.automation = eng
        await eng.start()
        sid = await new_session(server, d)
        provider = server.providers[0]
        # background turn
        await cmd(server, "/bg say hi in the background", sid)
        await until(lambda: provider.calls >= 1)
        # cron job
        n = provider.calls
        await cmd(server, '/schedule add "* * * * *" "cron says hi" --name hi', sid)
        await clock.advance(61)
        await until(lambda: provider.calls > n)
        # loop tick
        n = provider.calls
        await cmd(server, "/loop 5m watch the build --times 1", sid)
        await until(lambda: provider.calls > n)
        await asyncio.sleep(0.3)
        out = (await cmd(server, "/stats day 1", sid))["output"]
        row = server.usage.aggregate("day")[0]
        await eng.stop()
        want = {"background_turn", "cron_job", "loop_tick"}
        kinds = set(row["by_kind"])
        calls = server.usage._db.execute("SELECT task_kind, tier, count(*) FROM events WHERE kind='call' "
                                         "GROUP BY 1,2").fetchall()
        tiers = {k: t for k, t, _ in calls}
        if not (want <= kinds and all(tiers.get(k) == "cheap" for k in want) and "cheap" in out):
            raise AssertionError(f"kinds={kinds} tiers={tiers}\n{out}")
        return f"/stats output:\n{out}\ncall rows (kind,tier,n): {calls}"

    try:
        ev = asyncio.run(go())
        emit(M, crit, "gateway + AutomationEngine + fake provider: /bg, /schedule add (fake clock), /loop, then /stats",
             "PASS", ev)
    except Exception as e:  # noqa: BLE001
        emit(M, crit, "gateway + AutomationEngine + fake provider", "FAIL", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 4. degradation cost
PRICES = {  # USD per 1M tokens (in, out): illustrative, labelled as such in the evidence
    "cheap": (0.15, 0.60), "fast": (0.10, 0.40), "main": (3.0, 15.0), "strong": (15.0, 75.0)}


def degradation() -> None:
    crit = "Degradation cuts cost >=30% at the same pass rate"
    import json as _j

    from test_autonomy_gateway import call, make, run_turn

    verdict = {"type": "text", "match": "You classify a coding task", "text": _j.dumps(
        {"scope": "trivial", "needs_plan": False, "risk": "low", "parallelizable": False,
         "suggested_subtasks": [], "reason": "tiny"})}
    steps = [verdict, {"type": "text", "text": "DONE"}, {"type": "usage", "prompt_tokens": 1000,
                                                          "completion_tokens": 200}]
    n_inter, n_bg = 5, 5

    async def workload(name: str, **cfg) -> dict:
        d = sub(name)
        server = make(d, MP(), steps, mode="auto", **cfg)
        passed = 0
        for i in range(n_inter):
            await call(server, "session.create", {"cwd": str(d)})  # fresh session: no loop-guard repeats
            await asyncio.wait_for(run_turn(server, f"interactive task {i}"), 30)
            passed += server.session.stored.messages[-1]["content"].strip().endswith("DONE")
        for i in range(n_bg):
            await call(server, "session.create", {"cwd": str(d)})
            await asyncio.wait_for(run_turn(server, f"background job {i}", background=True), 30)
            passed += server.session.stored.messages[-1]["content"].strip().endswith("DONE")
        tot, calls = 0.0, {}
        for tier, c in server.usage.aggregate("day")[0]["by_tier"].items():
            pi, po = PRICES[tier]
            cost = c["tokens_in"] * pi / 1e6 + c["tokens_out"] * po / 1e6
            tot += cost
            calls[tier] = (c["calls"], c["tokens_in"], c["tokens_out"], round(cost, 5))
        return {"cost": tot, "pass": passed, "tasks": n_inter + n_bg, "by_tier": calls}

    async def go():
        base = await workload("deg-base", task_tiers={"classification": "main", "background_turn": "main"})
        deg = await workload("deg-on")
        return base, deg

    base, deg = asyncio.run(go())
    cut = 100 * (1 - deg["cost"] / base["cost"])
    same = base["pass"] == deg["pass"] == base["tasks"]
    table = ["tier   price/Mtok(in,out)  baseline(calls,tin,tout,$)   degraded(calls,tin,tout,$)"]
    for t in PRICES:
        if t in base["by_tier"] or t in deg["by_tier"]:
            table.append(f"{t:<6} {PRICES[t]!s:<18} {base['by_tier'].get(t, '-')!s:<28} {deg['by_tier'].get(t, '-')}")
    ev = (f"workload: {n_inter} interactive tasks (scope-gate classification + execution) + {n_bg} unattended "
          f"background turns, 1000/200 tokens per call. pass {base['pass']}/{base['tasks']} baseline vs "
          f"{deg['pass']}/{deg['tasks']} degraded. cost baseline(all main) ${base['cost']:.5f} vs degraded "
          f"${deg['cost']:.5f} = {cut:.0f}% cut (prices illustrative)\n" + "\n".join(table))
    status = "PASS" if cut >= 30 and same else "FAIL"
    emit(M, crit + " (fake provider)", "same scripted workload twice via gateway: task_tiers all-main vs default "
         "tier policy; cost = usage.db tokens x illustrative price table", status, ev)
    emit(M, crit + " (live comparison with real models)", "needs real model quality at both tiers", "PENDING",
         "fake comparison only proves routing/accounting; same-pass-rate on real tasks is untested: " + LIVE_DETAIL[:160],
         f"run a real 20-task benchmark twice (task_tiers all-main vs default) with live OmniRoute {RESET}, "
         "compare pass rate and /stats cost")


# ---------------------------------------------------------------- 5. /preview
def preview() -> None:
    crit = "/preview takes under 30 s"
    how = "live /preview on the fast tier (fake path timed for harness overhead)"
    from test_autonomy_gateway import call, make

    async def go() -> float:
        d = sub("preview")
        server = make(d, MP(), [{"type": "text", "model": "m-fast", "match": "sketch what the result",
                                 "text": "```\n[ todo ]\n```\nRisks:\n- a"}], mode="default")
        await call(server, "session.create", {"cwd": str(d)})
        t = time.monotonic()
        out = await call(server, "command.dispatch", {"name": "preview", "arg": "a CLI todo app"})
        assert out["tag"] == "preview"
        return time.monotonic() - t

    dt = asyncio.run(go())
    emit(M, crit, how, "PENDING",
         f"fake-provider /preview harness overhead {dt * 1000:.0f} ms (30 s hard timeout in preview.py); live not run: "
         f"{LIVE_DETAIL[:160]}",
         f"run `/preview a CLI todo app` against live OmniRoute fast tier and time it {RESET}")


# ---------------------------------------------------------------- 6. /ultraresearch
def ultraresearch() -> None:
    crit = "/ultraresearch yields >=10 resolving citations"
    how = "live /ultraresearch (fake-tools pipeline test run for harness correctness)"
    rc, out = lib.run(["uv", "run", "pytest", "-q", "tests/test_research.py"], cwd=CORE, timeout=300)
    dns = lib.run("getent hosts <searxng-host> || echo '<searxng-host>: no DNS answer'")[1].strip()
    mcp_note = "k3nox hub_searxng MCP is not configured in this temp home"
    reasons = []
    if not LIVE_OK:
        reasons.append(f"OmniRoute quota: {LIVE_DETAIL[:130]}")
    if "no DNS answer" in dns:
        reasons.append(f"SearXNG default URL unreachable ({dns}); {mcp_note}")
    emit(M, crit, how, "PENDING",
         f"fake-tools research tests rc={rc}: {tail(out, 1)}; blockers: {' | '.join(reasons) or 'none'}",
         f"on a host that resolves <searxng-host> (or set research.searxng_url / connect hub_searxng MCP) {RESET}, "
         "run /ultraresearch on a real topic and curl each cited URL (expect >=10 HTTP 2xx/3xx)")


# ---------------------------------------------------------------- 7. missed cron job
def missed_cron() -> None:
    crit = "A cron job missed while offline fires exactly once"
    from auto_helpers import FakeRunner, clock
    from k3code.automation.runner import RunResult
    from k3code.automation.scheduler import JobScheduler
    from k3code.automation.store import AutomationDB

    async def go() -> str:
        d = sub("missed")
        db_path = d / "automation.db"
        c, r = clock(), FakeRunner([RunResult(status="completed", text="done", api_calls=1)])
        s1 = JobScheduler(AutomationDB(db_path), r, c)
        s1.start()
        job = s1.add(prompt="p", schedule="*/5 * * * *", name="m")
        await s1.close()  # daemon goes offline
        c._now = job["next_run_at"] + 2 * 3600  # 24 slots missed
        s2 = JobScheduler(AutomationDB(db_path), r, c)  # daemon restarts on the persisted DB
        s2.start()
        await c.advance(1)
        first = len(r.prompts)
        await c.advance(1)
        await c.advance(1)
        again = len(r.prompts)
        runs = AutomationDB(db_path).runs(job["id"])
        await s2.close()
        if (first, again) != (1, 1) or [x["status"] for x in runs] != ["completed"]:
            raise AssertionError(f"fired {first} then {again}, runs={[x['status'] for x in runs]}")
        return f"24 slots missed (2 h of */5), after restart fired {first} time, still {again} after more ticks; run rows: {len(runs)}"

    emit(M, crit, "JobScheduler on a persisted automation.db: stop, jump fake clock 2 h, restart", "PASS",
         asyncio.run(go()))


# ---------------------------------------------------------------- 8. MCP schemas
SERVER_SRC = '''
from mcp.server.mcpserver import MCPServer
mcp = MCPServer("big")
def make(i):
    def f(path: str, count: int = 1, recursive: bool = False, pattern: str = "*") -> str:
        return "ok"
    f.__name__ = f"tool_{i:03d}"
    f.__doc__ = f"Tool number {i}: performs operation {i} on a path, optionally recursive, with a glob pattern and a repeat count, returning a status string."
    return f
for i in range(300):
    mcp.tool()(make(i))
if __name__ == "__main__":
    mcp.run("stdio")
'''


def mcp_context() -> None:
    crit = "MCP schemas <15% of context with deferred loading (300-tool fake MCP server)"
    from k3code.config import McpServerConfig
    from k3code.extratools import mcp_prompt, register_mcp_tools
    from k3code.mcpclient import McpManager
    from k3code.tools import build_registry

    srv = sub("mcp") / "big_server.py"
    srv.write_text(SERVER_SRC)
    window = 128_000

    def toks(specs, extra: str = "") -> int:
        blob = json.dumps([{"name": s.name, "description": s.description, "parameters": s.parameters} for s in specs])
        return (len(blob) + len(extra)) // 4  # ~4 chars/token estimate (no tokenizer dependency)

    async def go() -> str:
        mgr = McpManager({"big": McpServerConfig(command=sys.executable, args=[str(srv)])})
        try:
            await mgr.ensure_started()
            n = len(mgr.tools())
            if n != 300:
                raise AssertionError(f"server exposed {n} tools, wanted 300")
            reg = build_registry()
            base = toks(reg.specs())
            register_mcp_tools(reg, mgr)
            prompt = mcp_prompt(mgr)
            deferred = toks(reg.specs(), prompt) - base
            # a realistic turn: model loads 3 tools through mcp_tool_search
            _, search = reg.get("mcp_tool_search")
            await search({"query": "tool_007 tool_042 tool_199"})
            after3 = toks(reg.specs(), prompt) - base
            reg.activate([t.qualified for t in mgr.tools()])
            eager = toks(reg.specs()) - base
            named = prompt.count("mcp__big__")
            return (deferred, after3, eager, named)  # type: ignore[return-value]
        finally:
            await mgr.close()

    deferred, after3, eager, named = asyncio.run(go())
    pct = 100 * deferred / window
    ev = (f"300 tools; MCP overhead (tool specs + prompt names) vs {window}-token window, ~4 chars/token estimate: "
          f"deferred {deferred} tok = {pct:.1f}% (after loading 3 tools: {after3} tok = {100 * after3 / window:.1f}%); "
          f"eager all-schemas would be {eager} tok = {100 * eager / window:.1f}%. prompt lists {named} of 300 names "
          f"(limit 150; the rest are reachable via mcp_tool_search keywords)")
    emit(M, crit, "real McpManager + stdio server with 300 generated tools; register_mcp_tools + mcp_prompt; "
         "serialized spec size / window", "PASS" if pct < 15 and eager > deferred else "FAIL", ev)


def main() -> None:
    print(f"quota probe: {LIVE_OK} {LIVE_DETAIL[:120]}", file=sys.stderr)
    for name, fn in [("scope eval", scope_eval), ("fan-out", fanout), ("stats tiers", stats_tiers),
                     ("degradation", degradation), ("preview", preview), ("ultraresearch", ultraresearch),
                     ("missed cron", missed_cron), ("mcp context", mcp_context)]:
        safe(f"M4 {name}", fn)
    with contextlib.suppress(OSError):
        subprocess.run(["rm", "-rf", str(TMP)], check=False)


if __name__ == "__main__":
    main()
