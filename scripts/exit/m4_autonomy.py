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
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CORE = REPO / "core"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CORE / "tests"))
sys.path.insert(0, str(CORE / "src"))

try:
    import mcp  # noqa: F401

    import k3code  # noqa: F401
except ImportError:
    os.execvp("uv", ["uv", "run", "--project", str(CORE), "python", __file__, *sys.argv[1:]])

import lib  # noqa: E402
from lib import emit, tail  # noqa: E402

M = "M4"
TMP = Path(tempfile.mkdtemp(prefix="exit-m4-"))
os.environ["K3CODE_HOME"] = str(TMP / "home")  # never touch the real home
os.environ.pop("K3CODE_FAKE_PROVIDER", None)
BACKEND = lib.live_backend()  # Claude Code login by default (OmniRoute paused by owner); shared by every live row
LIVE_OK, LIVE_DETAIL = BACKEND["ok"], BACKEND["detail"]
RESET = "once a live model is available (Claude Code login, or K3_ALLOW_OMNIROUTE=1 with a working key)"


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


def _score_scope(rows: list[dict]) -> tuple[int, list[str], list[str], dict[tuple[str, str], int]]:
    """Run the product's classifier prompt + parser + danger floor over ``rows`` on the live backend.

    Returns (hits, misses, errors, confusion) where confusion[(want, got)] counts rows; ``got`` is "unparsed" when the
    reply was unusable (the product would then use the ``small`` fallback; here it counts as a miss).
    """
    from k3code.autonomy.scope import apply_floor, classifier_messages, parse_reply, repo_summary

    summary = repo_summary(REPO)
    hit, misses, errors = 0, [], []
    matrix: dict[tuple[str, str], int] = {}
    for r in rows:
        msgs = classifier_messages(r["prompt"], summary, "")
        try:
            text = lib.live_chat(BACKEND, [{"role": m.role, "content": m.content} for m in msgs])
        except Exception as e:  # noqa: BLE001 - a provider error must not abort the other rows
            errors.append(f"#{r['id']} {type(e).__name__}: {str(e)[:80]}")
            continue
        v, why = parse_reply(text)
        v = apply_floor(v, r["prompt"]) if v else None
        got = v.scope if v else "unparsed"
        matrix[(r["scope"], got)] = matrix.get((r["scope"], got), 0) + 1
        if got == r["scope"]:
            hit += 1
        else:
            misses.append(f"#{r['id']} want {r['scope']} got {got}" + ("" if v else f" ({why})"))
    return hit, misses, errors, matrix


def _fmt_matrix(matrix: dict[tuple[str, str], int]) -> str:
    from k3code.autonomy.scope import SCOPES

    cols = [*SCOPES, "unparsed"]
    head = "want\\got  " + " ".join(f"{c[:8]:>8}" for c in cols)
    lines = [head] + [f"{w:<9} " + " ".join(f"{matrix.get((w, c), 0):>8}" for c in cols) for w in SCOPES]
    return "confusion matrix (rows = label, columns = classifier):\n" + "\n".join(lines)


def scope_blind() -> None:
    """Generalisation check on scope_eval_blind.jsonl (40 rows). Prints agreement; never an exit row.

    The blind set was written before the classifier prompt was tuned; never tune the prompt against it.
    """
    from k3code.autonomy.scope import SCOPES

    rows = [json.loads(x) for x in (HERE / "scope_eval_blind.jsonl").read_text().splitlines() if x.strip()]
    assert len(rows) == 40 and all(r["scope"] in SCOPES for r in rows)
    if not LIVE_OK:
        print(f"scope blind: not run, no live backend: {LIVE_DETAIL}")
        return
    hit, misses, errors, matrix = _score_scope(rows)
    scored = len(rows) - len(errors)
    print(
        f"scope blind ({BACKEND['label']}): agreement {hit}/{scored} = {100 * hit / max(scored, 1):.0f}%"
        f" (errors: {len(errors)})\nmisses: {'; '.join(misses) or 'none'}\n{_fmt_matrix(matrix)}"
    )


# ---------------------------------------------------------------- 1. scope eval
def scope_eval() -> None:
    crit = "30-task scope eval: classifier agrees with labels on >=80%"
    rows = [json.loads(x) for x in (HERE / "scope_eval.jsonl").read_text().splitlines() if x.strip()]
    from k3code.autonomy.scope import SCOPES

    assert len(rows) == 30 and all(r["scope"] in SCOPES and r["label_source"] == "proposed-by-claude" for r in rows)
    dist = {s: sum(r["scope"] == s for r in rows) for s in SCOPES}
    how = "scripts/exit/scope_eval.jsonl (30 labels, proposed-by-claude) through the product's scope classifier prompt"
    if not LIVE_OK:
        emit(
            M,
            crit,
            how,
            "PENDING",
            f"eval set written and validated (30 rows, label dist {dist}); live classifier not run: {LIVE_DETAIL}",
            f"the owner confirms/edits the labels in scope_eval.jsonl, then re-run scripts/exit/m4_autonomy.py {RESET}",
        )
        return
    hit, misses, errors, matrix = _score_scope(rows)
    scored = len(rows) - len(errors)
    pct = 100 * hit / max(scored, 1)
    ev = (
        f"agreement {hit}/{scored} = {pct:.0f}% (target >=80%); misses: {'; '.join(misses) or 'none'}\n"
        + _fmt_matrix(matrix)
        + (f"; provider errors on {len(errors)} rows: {'; '.join(errors[:3])}" if errors else "")
    )
    how_live = f"{how} (live: {BACKEND['label']})"
    if errors:
        emit(M, crit, how_live, "PENDING", "incomplete live run: " + ev, f"re-run scripts/exit/m4_autonomy.py {RESET}")
    elif pct < 80:
        emit(M, crit, how_live, "FAIL", ev)
    else:
        # The labels were proposed by Claude and a Claude classifier agreeing with them is circular until the owner
        # confirms them, so a good score stays PENDING.
        emit(
            M,
            crit,
            how_live,
            "PENDING",
            ev + ". Provisional PASS: the labels in scope_eval.jsonl are proposed-by-claude and not yet confirmed.",
            "the owner confirms/edits the 30 labels in scripts/exit/scope_eval.jsonl; then re-run "
            "scripts/exit/m4_autonomy.py and this row counts",
        )


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
        "\n".join(ln for ln in out.splitlines() if "merged" in ln or "Final tests" in ln or "Budget" in ln), 4
    )
    emit(
        M,
        crit,
        "cd core && uv run python scripts/demo_ultracode.py (fake provider, 3 parallel worktree workers)",
        "PASS" if ok else "FAIL",
        ev,
    )


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
        server = make(
            d,
            MP(),
            [{"type": "text", "text": "ok"}, {"type": "usage", "prompt_tokens": 100, "completion_tokens": 10}],
            mode="auto",
        )
        clock = FakeClock()
        eng = AutomationEngine(server, clock=clock, wait_online=noop)
        server.automation = eng
        await eng.start()
        sid = await new_session(server, d)

        def ncalls() -> int:
            return sum(len(p.log) for p in server.providers)

        # background turn
        await cmd(server, "/bg say hi in the background", sid)
        await until(lambda: ncalls() >= 1)
        # cron job
        n = ncalls()
        await cmd(server, '/schedule add "* * * * *" "cron says hi" --name hi', sid)
        await clock.advance(61)
        await until(lambda: ncalls() > n)
        # loop tick
        n = ncalls()
        await cmd(server, "/loop 5m watch the build --times 1", sid)
        await until(lambda: ncalls() > n)
        await asyncio.sleep(0.3)
        out = (await cmd(server, "/stats day 1", sid))["output"]
        row = server.usage.aggregate("day")[0]
        await eng.stop()
        want = {"background_turn", "cron_job", "loop_tick"}
        kinds = set(row["by_kind"])
        calls = server.usage._db.execute(
            "SELECT task_kind, tier, count(*) FROM events WHERE kind='call' GROUP BY 1,2"
        ).fetchall()
        tiers = {k: t for k, t, _ in calls}
        if not (want <= kinds and all(tiers.get(k) == "cheap" for k in want) and "cheap" in out):
            raise AssertionError(f"kinds={kinds} tiers={tiers}\n{out}")
        return f"/stats output:\n{out}\ncall rows (kind,tier,n): {calls}"

    try:
        ev = asyncio.run(go())
        emit(
            M,
            crit,
            "gateway + AutomationEngine + fake provider: /bg, /schedule add (fake clock), /loop, then /stats",
            "PASS",
            ev,
        )
    except Exception:  # noqa: BLE001
        import traceback

        emit(M, crit, "gateway + AutomationEngine + fake provider", "FAIL", traceback.format_exc())


# ---------------------------------------------------------------- 4. degradation cost
PRICES = {  # USD per 1M tokens (in, out): illustrative, labelled as such in the evidence
    "cheap": (0.15, 0.60),
    "fast": (0.10, 0.40),
    "main": (3.0, 15.0),
    "strong": (15.0, 75.0),
}


def degradation() -> None:
    crit = "Degradation cuts cost >=30% at the same pass rate"
    import json as _j

    from test_autonomy_gateway import call, make, run_turn

    verdict = {
        "type": "text",
        "match": "You classify a coding task",
        "text": _j.dumps(
            {
                "scope": "trivial",
                "needs_plan": False,
                "risk": "low",
                "parallelizable": False,
                "suggested_subtasks": [],
                "reason": "tiny",
            }
        ),
    }
    steps = [
        verdict,
        {"type": "text", "text": "DONE"},
        {"type": "usage", "prompt_tokens": 1000, "completion_tokens": 200},
    ]
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
    ev = (
        f"workload: {n_inter} interactive tasks (scope-gate classification + execution) + {n_bg} unattended "
        f"background turns, 1000/200 tokens per call. pass {base['pass']}/{base['tasks']} baseline vs "
        f"{deg['pass']}/{deg['tasks']} degraded. cost baseline(all main) ${base['cost']:.5f} vs degraded "
        f"${deg['cost']:.5f} = {cut:.0f}% cut (prices illustrative)\n" + "\n".join(table)
    )
    status = "PASS" if cut >= 30 and same else "FAIL"
    emit(
        M,
        crit + " (fake provider)",
        "same scripted workload twice via gateway: task_tiers all-main vs default "
        "tier policy; cost = usage.db tokens x illustrative price table",
        status,
        ev,
    )
    if LIVE_OK and BACKEND["kind"] == "claude-cli":
        return  # degradation_live() emits the live row
    emit(
        M,
        crit + " (live comparison with real models)",
        "needs real model quality at both tiers",
        "PENDING",
        "fake comparison only proves routing/accounting; "
        "same-pass-rate on real tasks is untested: " + LIVE_DETAIL[:160],
        f"run a real task benchmark twice (task_tiers all-main vs default) {RESET}, compare pass rate and /stats cost",
    )


LIVE_TASKS = [  # (background?, prompt, check(dir) -> bool); every task is tiny and mechanically checkable
    (False, "Create sq.py that prints the square of 7, then run it with bash.", lambda d: _py(d, "sq.py") == "49"),
    (
        False,
        "Create rev.py that prints the reverse of the string 'abc' (use slicing), then run it with bash.",
        lambda d: _py(d, "rev.py") == "cba",
    ),
    (
        False,
        "Create fizz.py that prints FizzBuzz for 1 to 15, one item per line, then run it with bash.",
        lambda d: _py(d, "fizz.py").splitlines()[-1:] == ["FizzBuzz"] and _py(d, "fizz.py").splitlines()[2] == "Fizz",
    ),
    (
        False,
        "Create wc.py that prints the number of words in the string 'one two three four', then run it with bash.",
        lambda d: _py(d, "wc.py") == "4",
    ),
    # "prints 6 factorial" does not fix the format: "720" and "6! = 720" are both right,
    # so the check looks for the value
    (
        False,
        "Create fact.py that prints 6 factorial, then run it with bash.",
        lambda d: "720" in _py(d, "fact.py").replace("=", " ").split(),
    ),
    (
        True,
        "Create a.txt containing exactly the word alpha.",
        lambda d: (d / "a.txt").exists() and (d / "a.txt").read_text().strip() == "alpha",
    ),
    (
        True,
        "Create nums.txt with the numbers 1 to 5, one per line.",
        lambda d: (d / "nums.txt").exists() and (d / "nums.txt").read_text().split() == list("12345"),
    ),
    (
        True,
        "Create upper.py that prints HELLO by upper-casing the string 'hello', then run it with bash.",
        lambda d: _py(d, "upper.py") == "HELLO",
    ),
    (
        True,
        "Create sum.py that prints the sum of the integers 1 to 10, then run it with bash.",
        lambda d: _py(d, "sum.py") == "55",
    ),
    (
        True,
        "Create ver.txt containing exactly the text v1.2.3.",
        lambda d: (d / "ver.txt").exists() and (d / "ver.txt").read_text().strip() == "v1.2.3",
    ),
]


def _py(d: Path, name: str) -> str:
    """Output of `python3 <name>` in d ('' when missing or failing): the check re-runs the file itself."""
    if not (d / name).exists():
        return ""
    r = subprocess.run(["python3", name], cwd=d, capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else ""


def degradation_live() -> None:
    """Real-model comparison: the same 10 small tasks with the default tier policy and with everything on `main`."""
    if not (LIVE_OK and BACKEND["kind"] == "claude-cli"):
        return
    crit = "Degradation cuts cost >=30% at the same pass rate"
    from test_autonomy_gateway import GatewayServer, ProviderEntry, SessionStore, Settings, call, run_turn

    def live_server(d: Path, **cfg):
        os.environ["K3CODE_HOME"] = str(d / "home")
        os.environ.pop("K3CODE_FAKE_PROVIDER", None)  # an earlier fake row's shim leaves it set
        provider = ProviderEntry(
            name="claude-code",
            kind="claude-cli",
            models={"default": lib.LIVE_DEFAULT_MODEL},
            tiers={"strong": lib.LIVE_DEFAULT_MODEL, "cheap": lib.LIVE_CHEAP_MODEL, "fast": lib.LIVE_CHEAP_MODEL},
        )
        server = GatewayServer(
            config=Settings(providers=[provider], permission_mode="auto", **cfg), store=SessionStore(d / "sessions.db")
        )
        frames: list[str] = []
        server._write = frames.append  # type: ignore[method-assign]
        server._frames = frames  # type: ignore[attr-defined]
        return server

    tasks = [LIVE_TASKS[0], LIVE_TASKS[5]] if os.environ.get("M4_LIVE_SMOKE") == "1" else LIVE_TASKS  # 2-task dry run

    async def workload(name: str, **cfg) -> dict:
        root = sub(name)
        server = live_server(root, **cfg)
        passed, failed = 0, []
        for i, (background, prompt, check) in enumerate(tasks):
            d = root / f"task{i}"
            d.mkdir()
            await call(server, "session.create", {"cwd": str(d)})
            try:
                await asyncio.wait_for(run_turn(server, prompt, [{"choice": "once"}] * 30, background=background), 300)
            except Exception as e:  # noqa: BLE001 - a stuck or failed task counts as not passed
                failed.append(f"#{i} {type(e).__name__}: {str(e)[:60]}")
                continue
            if check(d):
                passed += 1
            else:
                failed.append(f"#{i} check failed")
        agg = server.usage.aggregate("day")[0]
        tiers = {
            t: (c["calls"], c["tokens_in"], c["tokens_out"], round(c.get("cost_usd", 0.0), 4))
            for t, c in agg["by_tier"].items()
        }
        return {
            "cost": agg["cost_usd"] or 0.0,
            "pass": passed,
            "tasks": len(tasks),
            "by_tier": tiers,
            "failed": failed,
            "calls": agg["calls"],
        }

    def merged(a: dict, b: dict) -> dict:
        tiers = {}
        for t in set(a["by_tier"]) | set(b["by_tier"]):
            x, y = a["by_tier"].get(t, (0, 0, 0, 0.0)), b["by_tier"].get(t, (0, 0, 0, 0.0))
            tiers[t] = tuple(round(p + q, 4) for p, q in zip(x, y, strict=True))
        return {
            "cost": a["cost"] + b["cost"],
            "pass": a["pass"] + b["pass"],
            "tasks": a["tasks"] + b["tasks"],
            "by_tier": tiers,
            "failed": [*a["failed"], *b["failed"]],
            "calls": a["calls"] + b["calls"],
        }

    async def go():
        # two runs of the 10 tasks per config: with ~5 % per-task noise one run cannot tell "same pass rate".
        # "no degradation" = every kind on main, and trivial interactive tasks not routed to the cheap tier either
        base_cfg = {
            "task_tiers": {"classification": "main", "background_turn": "main"},
            "autonomy": {"degrade_trivial": False},
        }
        base = merged(await workload("deg-live-base-1", **base_cfg), await workload("deg-live-base-2", **base_cfg))
        deg = merged(await workload("deg-live-on-1"), await workload("deg-live-on-2"))
        return base, deg

    base, deg = asyncio.run(go())
    cut = 100 * (1 - deg["cost"] / base["cost"]) if base["cost"] else 0.0
    # "same pass rate" over 20 runs per config: the degraded config may lose at most one run to noise (5 %)
    same = deg["pass"] >= base["pass"] - 1
    table = ["tier   baseline(calls,tin,tout,list$)       degraded(calls,tin,tout,list$)"]
    for t in sorted(set(base["by_tier"]) | set(deg["by_tier"])):
        table.append(f"{t:<6} {base['by_tier'].get(t, '-')!s:<36} {deg['by_tier'].get(t, '-')}")
    ev = "\n".join(
        [  # exactly 6 lines: emit() keeps the last six
            f"{base['tasks']} task runs per config (the same 10 small tasks twice: 5 interactive with scope gate, 5 "
            f"unattended background) on {BACKEND['label']}",
            f"pass {base['pass']}/{base['tasks']} baseline (all main) vs {deg['pass']}/{deg['tasks']} degraded; list-price "
            f"cost ${base['cost']:.4f} vs ${deg['cost']:.4f} = {cut:.0f}% cut; calls {base['calls']} vs {deg['calls']}",
            f"failed tasks: baseline {base['failed'] or 'none'}; degraded {deg['failed'] or 'none'}",
            *table,
        ]
    )
    status = "PASS" if cut >= 30 and same and base["pass"] >= base["tasks"] - 2 else "FAIL"
    emit(
        M,
        crit + " (live comparison with real models)",
        "same 10 real tasks twice via the gateway (task_tiers all-main vs default policy), pass = the produced "
        "file/output is checked by the script; cost = usage.db cost_usd (provider-reported list price)",
        status,
        ev,
    )


# ---------------------------------------------------------------- 5. /preview
def preview() -> None:
    crit = "/preview takes under 30 s"
    how = "live /preview on the fast tier (fake path timed for harness overhead)"
    from test_autonomy_gateway import call, make

    async def go() -> float:
        d = sub("preview")
        server = make(
            d,
            MP(),
            [
                {
                    "type": "text",
                    "model": "m-fast",
                    "match": "sketch what the result",
                    "text": "```\n[ todo ]\n```\nRisks:\n- a",
                }
            ],
            mode="default",
        )
        await call(server, "session.create", {"cwd": str(d)})
        t = time.monotonic()
        out = await call(server, "command.dispatch", {"name": "preview", "arg": "a CLI todo app"})
        assert out["tag"] == "preview"
        return time.monotonic() - t

    dt = asyncio.run(go())
    overhead = f"fake-provider /preview harness overhead {dt * 1000:.0f} ms (30 s hard timeout in preview.py)"
    if not LIVE_OK:
        emit(
            M,
            crit,
            how,
            "PENDING",
            f"{overhead}; live not run: {LIVE_DETAIL[:160]}",
            f"run `/preview a CLI todo app` against the live fast tier and time it {RESET}",
        )
        return
    # The real-TUI timing needs pexpect + pyte, which this uv env lacks: run it as its own script.
    rc, out = lib.run(["python3", str(HERE / "preview_live.py")], timeout=240)
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        emit(M, crit, how, "FAIL", f"{overhead}\npreview_live.py rc={rc}: {tail(out, 4)}")
        return
    if "skipped" in res:
        emit(
            M,
            crit,
            how,
            "PENDING",
            f"{overhead}; live not run: {res['skipped']}",
            f"run `/preview a CLI todo app` against the live fast tier and time it {RESET}",
        )
        return
    status = "PASS" if res["ok"] and res["sketch"] and res["secs"] < 30 else "FAIL"
    emit(
        M,
        crit,
        f"real TUI session, `/preview a CLI todo app` timed from Enter to the finished sketch; "
        f"fast tier = {res['label']}",
        status,
        f"{overhead}\n{res['tail']}\n/preview took {res['secs']} s (limit 30 s); "
        f"sketch with Risks shown: {res['sketch']}",
    )


# ---------------------------------------------------------------- 6. /ultraresearch
def ultraresearch_live() -> tuple[str, str]:
    """Real /ultraresearch (live model, keyless DuckDuckGo search), then GET every cited URL.

    A cited URL "resolves" when its server answers with a page (HTTP < 400) or refuses a script (401/403/429: the page
    exists, the site just blocks bots, e.g. StackOverflow). 404/410/5xx and connection errors do not resolve.
    """
    import httpx

    from test_autonomy_gateway import GatewayServer, ProviderEntry, SessionStore, Settings, call

    d = sub("research-live")
    os.environ["K3CODE_HOME"] = str(d / "home")
    os.environ.pop("K3CODE_FAKE_PROVIDER", None)
    provider = ProviderEntry(
        name="claude-code",
        kind="claude-cli",
        models={"default": lib.LIVE_DEFAULT_MODEL},
        tiers={"strong": lib.LIVE_DEFAULT_MODEL, "cheap": lib.LIVE_CHEAP_MODEL, "fast": lib.LIVE_CHEAP_MODEL},
    )
    # searxng_url "" = no SearXNG: the built-in tools search through the keyless DuckDuckGo fallback
    server = GatewayServer(
        config=Settings(providers=[provider], permission_mode="auto", research={"searxng_url": ""}),
        store=SessionStore(d / "sessions.db"),
    )
    frames: list[str] = []
    server._write = frames.append  # type: ignore[method-assign]
    server._frames = frames  # type: ignore[attr-defined]
    question = "How do Python asyncio TaskGroups differ from asyncio.gather, and when should each be used?"

    async def go():
        await call(server, "session.create", {"cwd": str(d)})
        t0 = time.monotonic()
        res = await server.research.run(server.session, question)
        return res, time.monotonic() - t0

    res, secs = asyncio.run(go())
    by_id = {s.id: s for s in res.state.sources}
    urls = [by_id[i].url for i in res.cited if i in by_id]
    ok = blocked = 0
    bad: list[str] = []
    agent = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    with httpx.Client(follow_redirects=True, timeout=25.0, headers=agent) as c:
        for u in urls:
            try:
                r = c.get(u)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{type(e).__name__} {u[:60]}")
                continue
            if r.status_code < 400:
                ok += 1
            elif r.status_code in (401, 403, 429):
                blocked += 1
            else:
                bad.append(f"{r.status_code} {u[:60]}")
    resolving = ok + blocked
    status = "PASS" if resolving >= 10 and not bad else "FAIL"
    ev = "\n".join(
        [
            f"question: {question}",
            f"{len(res.state.sources)} sources read, {len(urls)} cited, {resolving} resolve ({ok} HTTP 2xx/3xx + {blocked} "
            f"answered 401/403/429 to a script) in a {secs:.0f} s run via {res.tools}",
            f"broken (404/5xx/unreachable): {bad or 'none'}",
            f"report saved: {res.path}",
        ]
    )
    return status, ev


def ultraresearch() -> None:
    crit = "/ultraresearch yields >=10 resolving citations"
    how = "live /ultraresearch (fake-tools pipeline test run for harness correctness)"
    rc, out = lib.run(["uv", "run", "pytest", "-q", "tests/test_research.py"], cwd=CORE, timeout=300)
    if LIVE_OK and BACKEND["kind"] == "claude-cli":
        status, ev = ultraresearch_live()
        emit(
            M,
            crit,
            f"real /ultraresearch on a real question: {BACKEND['label']}; keyless DuckDuckGo search; "
            f"every cited URL fetched (pipeline tests rc={rc})",
            status,
            ev,
        )
        return
    dns = lib.run("getent hosts <searxng-host> || echo '<searxng-host>: no DNS answer'")[1].strip()
    mcp_note = "k3nox hub_searxng MCP is not configured in this temp home"
    reasons = []
    if not LIVE_OK:
        reasons.append(f"OmniRoute quota: {LIVE_DETAIL[:130]}")
    if "no DNS answer" in dns:
        reasons.append(f"SearXNG default URL unreachable ({dns}); {mcp_note}")
    emit(
        M,
        crit,
        how,
        "PENDING",
        f"fake-tools research tests rc={rc}: {tail(out, 1)}; blockers: {' | '.join(reasons) or 'none'}",
        f"on a host that resolves <searxng-host> (or set research.searxng_url / connect hub_searxng MCP) {RESET}, "
        "run /ultraresearch on a real topic and curl each cited URL (expect >=10 HTTP 2xx/3xx)",
    )


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
        return (
            f"24 slots missed (2 h of */5), after restart fired {first} time, "
            f"still {again} after more ticks; run rows: {len(runs)}"
        )

    emit(
        M,
        crit,
        "JobScheduler on a persisted automation.db: stop, jump fake clock 2 h, restart",
        "PASS",
        asyncio.run(go()),
    )


# ---------------------------------------------------------------- 8. MCP schemas
SERVER_SRC = """
from mcp.server.mcpserver import MCPServer
mcp = MCPServer("big")
def make(i):
    def f(path: str, count: int = 1, recursive: bool = False, pattern: str = "*") -> str:
        return "ok"
    f.__name__ = f"tool_{i:03d}"
    f.__doc__ = f"Tool number {i}: performs operation {i} on a path, optionally recursive, \
with a glob pattern and a repeat count, returning a status string."
    return f
for i in range(300):
    mcp.tool()(make(i))
if __name__ == "__main__":
    mcp.run("stdio")
"""


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
    ev = (
        f"300 tools; MCP overhead (tool specs + prompt names) vs {window}-token window, ~4 chars/token estimate: "
        f"deferred {deferred} tok = {pct:.1f}% (after loading 3 tools: {after3} tok = {100 * after3 / window:.1f}%); "
        f"eager all-schemas would be {eager} tok = {100 * eager / window:.1f}%. prompt lists {named} of 300 names "
        f"(limit 150; the rest are reachable via mcp_tool_search keywords)"
    )
    emit(
        M,
        crit,
        "real McpManager + stdio server with 300 generated tools; register_mcp_tools + mcp_prompt; "
        "serialized spec size / window",
        "PASS" if pct < 15 and eager > deferred else "FAIL",
        ev,
    )


def main() -> None:
    print(f"live backend: {BACKEND['kind']} ok={LIVE_OK} {LIVE_DETAIL[:120]}", file=sys.stderr)
    want = sys.argv[1:]  # e.g. `m4_autonomy.py preview scope_eval` runs only those rows
    if want == ["blind"]:  # `m4_autonomy.py blind`: agreement on the blind scope set (not an exit row)
        scope_blind()
        return
    for name, fn in [
        ("scope eval", scope_eval),
        ("fan-out", fanout),
        ("stats tiers", stats_tiers),
        ("degradation", degradation),
        ("degradation live", degradation_live),
        ("preview", preview),
        ("ultraresearch", ultraresearch),
        ("missed cron", missed_cron),
        ("mcp context", mcp_context),
    ]:
        if want and name.replace(" ", "_").replace("-", "_") not in want:
            continue
        safe(f"M4 {name}", fn)
    with contextlib.suppress(OSError):
        subprocess.run(["rm", "-rf", str(TMP)], check=False)


if __name__ == "__main__":
    main()
