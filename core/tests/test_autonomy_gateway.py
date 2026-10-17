"""M4a end-to-end through the gateway with the scripted fake provider (model/match filters)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from k3code.config import ProviderEntry, Settings
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore

PLAN = (
    "## Goal\nAdd flag\n## Steps\n1. edit\n## Files\na.py\n## Risks\nnone\n## Verification\npytest\n## Estimate\nsmall"
)


def verdict(scope: str, *, risk: str = "low", needs_plan: bool = False, reason: str = "r") -> dict:
    return {
        "type": "text",
        "match": "You classify a coding task",
        "text": json.dumps(
            {
                "scope": scope,
                "needs_plan": needs_plan,
                "risk": risk,
                "parallelizable": False,
                "suggested_subtasks": [],
                "reason": reason,
            }
        ),
    }


def usage(model: str, match: str, p: int = 10, c: int = 5) -> dict:
    return {"type": "usage", "model": model, "match": match, "prompt_tokens": p, "completion_tokens": c}


PLANNER = [
    {
        "type": "tool_call",
        "model": "m-strong",
        "match": "PLANNING mode",
        "when": "first",
        "name": "exit_plan",
        "arguments": {"plan": PLAN},
    },
    usage("m-strong", "PLANNING mode", 40, 20),
]
ADVISOR_BRIEF = [
    {"type": "text", "model": "m-strong", "match": "brief critique", "text": "- watch edge case X"},
    usage("m-strong", "brief critique", 30, 10),
]
EXECUTOR = [
    {"type": "text", "model": "m-main", "match": "Approved plan", "text": "implemented"},
    usage("m-main", "Approved plan", 50, 15),
]
DIRECT = [
    {"type": "text", "model": "m-main", "match": "TRIVIAL-TASK", "text": "done directly"},
    usage("m-main", "TRIVIAL-TASK", 20, 5),
]
PROPOSER = [
    {
        "type": "text",
        "match": "one step ahead",
        "text": json.dumps(
            [{"kind": "also_setup", "text": "Do you want me to also set up CI?", "action": "set up CI for this repo"}]
        ),
    }
]


def sub(tmp: Path, name: str) -> Path:
    d = tmp / name
    d.mkdir()
    return d


def k3home(tmp: Path) -> Path:
    """The k3code home sits beside the test's project dir, never inside it: a sandboxed project may not contain it."""
    return tmp.parent / f"{tmp.name}-k3home"


def make(tmp: Path, monkeypatch, steps: list[dict], mode: str = "auto", **cfg: Any):
    monkeypatch.setenv("K3CODE_HOME", str(k3home(tmp)))
    script = tmp / "script.json"
    script.write_text(json.dumps(steps))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    provider = ProviderEntry(
        name="t",
        kind="openai",
        base_url="http://t",
        api_key_env="NOPE",
        models={"default": "m-main"},
        tiers={"strong": "m-strong", "cheap": "m-cheap", "fast": "m-fast"},
    )
    # the scripts answer one turn: auto-continue (on by default in auto/yolo) would add judge calls and turns
    cfg["autonomy"] = {"auto_continue": False, **(cfg.get("autonomy") or {})}
    server = GatewayServer(
        config=Settings(providers=[provider], permission_mode=mode, **cfg), store=SessionStore(tmp / "sessions.db")
    )
    frames: list[str] = []
    server._write = frames.append  # type: ignore[method-assign]
    server._frames = frames  # type: ignore[attr-defined]
    return server


async def call(server: GatewayServer, method: str, params: dict | None = None) -> dict:
    n = len(server._frames)  # type: ignore[attr-defined]
    await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}))
    out = [json.loads(x) for x in server._frames[n:] if json.loads(x).get("id") == 7]  # type: ignore[attr-defined]
    return out[0]["result"]


def events(server: GatewayServer, kind: str) -> list[dict]:
    out = []
    for line in server._frames:  # type: ignore[attr-defined]
        f = json.loads(line)
        if f.get("method") == "event" and f["params"]["type"] == kind:
            out.append(f["params"]["payload"])
    return out


async def run_turn(server: GatewayServer, text: str, answers: list[dict] | None = None, **params: Any) -> list[dict]:
    """Submit a prompt, answering approval requests from ``answers``; returns the requests seen."""
    answers = answers or []
    seen: list[dict] = []
    await call(server, "prompt.submit", {"text": text, **params})
    task = server.session.turn_task
    for _ in range(1000):
        for line in list(server._frames):  # type: ignore[attr-defined]
            frame = json.loads(line)
            if frame.get("method") == "approval" and frame["id"] not in {r["id"] for r in seen}:
                seen.append(frame)
                answer = answers[len(seen) - 1] if len(seen) <= len(answers) else {"choice": "deny"}
                await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": answer}))
        if task.done():
            break
        await asyncio.sleep(0.01)
    await task
    await server.autonomy.drain()
    return seen


def models_called(server: GatewayServer) -> list[str]:
    return [c["model"] for p in server.providers for c in p.log]


async def start(server: GatewayServer, tmp: Path) -> None:
    await call(server, "session.create", {"cwd": str(tmp)})


def scope_rows(tmp: Path) -> list[dict]:
    path = k3home(tmp) / "scope_log.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


# ── plan-first ──


async def test_medium_in_auto_plans_on_strong_and_auto_approves(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("medium"), *PLANNER, *ADVISOR_BRIEF, *EXECUTOR, *PROPOSER])
    await start(server, tmp_path)
    seen = await run_turn(server, "add a --verbose flag to the CLI")
    assert seen == []  # low risk: no approval prompt
    called = models_called(server)
    assert called[0] == "m-cheap", called  # classification tier
    assert "m-main" in called, called
    assert called.index("m-strong") < called.index("m-main")  # plan before execution
    plans = events(server, "plan.show")
    assert [p["status"] for p in plans] == ["proposed", "approved"]
    assert plans[-1]["auto_approved"] is True and plans[-1]["sections"]["Goal"] == "Add flag"
    assert "advisor" not in plans[-1]  # a medium plan gets no critique (the strong call is for large/huge or high risk)
    assert "brief critique" not in "".join(c["text"] for p in server.providers for c in p.log)
    assert server.session.stored.messages[-1]["content"] == "implemented"
    # the executor saw the plan; planning ran read-only (no edit tools offered in plan mode)
    main_call = next(c for p in server.providers for c in p.log if c["model"] == "m-main")
    assert "Approved plan" in main_call["text"] and "Add flag" in main_call["text"]
    planner_call = next(c for p in server.providers for c in p.log if "PLANNING mode" in c["text"])
    assert "exit_plan" in planner_call["tools"]  # only offered in plan mode, where edits are denied
    assert "exit_plan" not in main_call["tools"]
    # scope log: verdict + outcome, only the hash of the prompt
    rows = scope_rows(tmp_path)
    assert [r["type"] for r in rows] == ["verdict", "outcome"]
    assert rows[1]["outcome"] == "done" and rows[1]["planned"] is True
    assert "verbose" not in json.dumps(rows)
    assert server.session.perms.mode.value == "auto"  # plan approval did not leave auto mode


async def test_large_and_high_risk_plans_get_the_advisor_critique(tmp_path, monkeypatch):
    """The brief strong-tier critique after approval costs a strong call per plan: only a large/huge or high-risk
    plan gets one (a medium plan is approved and run as is, see the test above)."""
    server = make(tmp_path, monkeypatch, [verdict("large"), *PLANNER, *ADVISOR_BRIEF, *EXECUTOR, *PROPOSER])
    await start(server, tmp_path)
    await run_turn(server, "add a --verbose flag to the CLI")
    plans = events(server, "plan.show")
    assert plans[-1]["status"] == "approved" and "watch edge case X" in plans[-1]["advisor"]
    steps = [verdict("small", risk="high", needs_plan=True), *PLANNER, *ADVISOR_BRIEF, *EXECUTOR]
    server2 = make(sub(tmp_path, "h"), monkeypatch, steps, autonomy={"proposals": False})
    await start(server2, tmp_path / "h")
    await run_turn(server2, "tidy things", [{"choice": "once"}])
    assert "watch edge case X" in events(server2, "plan.show")[-1]["advisor"]


async def test_medium_risk_in_auto_is_approved_without_asking(tmp_path, monkeypatch):
    """Only high risk asks in auto mode: a medium-risk plan is auto-approved like a low-risk one."""
    steps = [verdict("medium", risk="med"), *PLANNER, *EXECUTOR]
    server = make(tmp_path, monkeypatch, steps, autonomy={"proposals": False})
    await start(server, tmp_path)
    seen = await run_turn(server, "add a --verbose flag to the CLI")
    assert seen == []
    plans = events(server, "plan.show")
    assert [p["status"] for p in plans] == ["proposed", "approved"] and plans[-1]["auto_approved"] is True
    assert plans[-1]["risk"] == "med"
    assert server.session.stored.messages[-1]["content"] == "implemented"


async def test_planner_of_an_auto_session_reads_outside_the_project_as_auto_mode_does(tmp_path, monkeypatch):
    """The planning turn ran in plan mode, which asks a person for every read outside the project: an auto session got
    an approval prompt for a planner read that its own turns refuse without asking. The planner now decides such reads
    as the session does: refused with the /add-dir hint (credential paths stay hard-denied, see test_engine)."""
    proj = sub(tmp_path, "proj")
    (sub(tmp_path, "other") / "notes.md").write_text("elsewhere\n")
    planner_call = {"type": "tool_call", "model": "m-strong", "match": "PLANNING mode"}
    steps = [
        verdict("medium"),
        {**planner_call, "when": "first", "name": "read", "arguments": {"path": "../other/notes.md"}},
        {**planner_call, "when": "after_tool", "name": "exit_plan", "arguments": {"plan": PLAN}},
        *EXECUTOR,
    ]
    server = make(proj, monkeypatch, steps, autonomy={"proposals": False})
    await start(server, proj)
    seen = await run_turn(server, "add a --verbose flag to the CLI")
    assert seen == []  # no approval prompt in auto mode
    planner_texts = [c["text"] for p in server.providers for c in p.log if "PLANNING mode" in c["text"]]
    assert any("outside the project" in t and "/add-dir" in t for t in planner_texts)
    assert not any("elsewhere" in t for t in planner_texts)  # the file was not read
    assert server.session.stored.messages[-1]["content"] == "implemented"


def test_planner_permissions_follow_the_session(tmp_path):
    from k3code.autonomy.plan_first import planner_permissions
    from k3code.permissions import PermissionMode, Rule
    from k3code.permissions.state import PermissionState

    session = PermissionState(mode=PermissionMode.AUTO, cwd=tmp_path / "proj", add_dirs=[str(tmp_path / "lib")])
    perms = planner_permissions(session)
    assert perms.mode is PermissionMode.PLAN and perms.plan_for is PermissionMode.AUTO
    assert perms.add_dirs == session.add_dirs and perms.add_dirs is not session.add_dirs
    session.session_rules.append(Rule(tool="read", pattern="/srv/data/*", action="allow"))
    assert perms.decide("read", {"path": "/srv/data/a.csv"}).action == "allow"  # the session's approval holds
    assert perms.decide("read", {"path": "/srv/other/a.csv"}).action == "deny"
    assert perms.decide("read", {"path": "/etc/k3code-test.conf"}).action == "allow"  # as auto mode reads it
    # still read-only whatever a rule says, and credentials stay refused
    session.session_rules.append(Rule(tool="bash", pattern="*", action="allow"))
    assert perms.decide("bash", {"command": "ls"}).action == "deny"
    assert perms.decide("write", {"path": "a.py", "content": "x"}).action == "deny"
    assert perms.decide("read", {"path": "~/.ssh/id_rsa"}).hardline == "sensitive-path"
    # a default-mode session's planner asks a person for both, as before
    plain = planner_permissions(PermissionState(mode=PermissionMode.DEFAULT, cwd=tmp_path / "proj"))
    for path in ("/srv/other/a.csv", "/etc/k3code-test.conf"):
        d = plain.decide("read", {"path": path})
        assert d.action == "ask" and d.needs_human, (path, d)


async def test_planner_gets_the_conversation_condensed_not_as_history(tmp_path, monkeypatch):
    """The planning turn carried the whole message history (every tool result in full) into the strong-tier call.
    It now gets the condensed transcript the advisor gets: clipped messages, the old part summarized on the cheap
    tier past ``advisor_compact_chars``."""
    steps = [
        verdict("medium"),
        {"type": "text", "model": "m-cheap", "match": "Summarize this coding conversation", "text": "SUMMARY"},
        *PLANNER,
        *EXECUTOR,
    ]
    cfg = {"advisor_compact_chars": 500, "proposals": False}
    server = make(tmp_path, monkeypatch, steps, autonomy=cfg)
    await start(server, tmp_path)
    server.session.messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "x" * 300} for i in range(10)
    ]
    await run_turn(server, "add a --verbose flag to the CLI")
    planner_call = next(c for p in server.providers for c in p.log if "PLANNING mode" in c["text"])
    assert "Conversation so far:" in planner_call["text"] and "SUMMARY" in planner_call["text"]
    assert "message 9 " in planner_call["text"]  # the recent tail stays verbatim
    assert "message 0 " not in planner_call["text"]  # the old part went in summarized, not as history


async def test_planner_sees_the_project_memory_the_executor_sees(tmp_path, monkeypatch):
    """The planning loop used the bare base prompt: the plan was written without K3CODE.md, and then drove a turn that
    had it. It gets the same composed prompt as the normal loop, with the planning addendum last."""
    (tmp_path / "K3CODE.md").write_text("TEST-CMD-FOR-PLANNER: run make check")
    skill = k3home(tmp_path) / "skills" / "demo-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo-skill\ndescription: A demo skill\n---\nbody")
    server = make(tmp_path, monkeypatch, [verdict("medium"), *PLANNER, *ADVISOR_BRIEF, *EXECUTOR, *PROPOSER])
    await start(server, tmp_path)
    await run_turn(server, "add a --verbose flag to the CLI")
    planner_call = next(c for p in server.providers for c in p.log if "PLANNING mode" in c["text"])
    main_call = next(c for p in server.providers for c in p.log if c["model"] == "m-main")
    for call_ in (planner_call, main_call):
        assert "## Project memory (K3CODE.md)" in call_["text"] and "TEST-CMD-FOR-PLANNER" in call_["text"]
    assert planner_call["text"].index("TEST-CMD-FOR-PLANNER") < planner_call["text"].index("PLANNING mode")
    # the planner has no skill tool, so it is not told about skills; the normal loop is
    assert "## Skills" in main_call["text"] and "## Skills" not in planner_call["text"]
    assert "skill" not in planner_call["tools"] and "skill" in main_call["tools"]


async def test_planner_is_not_told_about_an_advisor_tool_it_does_not_have(tmp_path, monkeypatch):
    """With /advisor on, the prompt names the ``advisor`` tool. Only the normal loop registers it; the planner must not
    be told to consult a tool it cannot call."""
    server = make(tmp_path, monkeypatch, [verdict("medium"), *PLANNER, *ADVISOR_BRIEF, *EXECUTOR, *PROPOSER])
    await start(server, tmp_path)
    await call(server, "command.dispatch", {"name": "advisor", "arg": "on"})
    await run_turn(server, "add a --verbose flag to the CLI")
    planner_call = next(c for p in server.providers for c in p.log if "PLANNING mode" in c["text"])
    main_call = next(c for p in server.providers for c in p.log if c["model"] == "m-main")
    assert "advisor" in main_call["tools"] and "advisor` tool" in main_call["text"]  # the control: the normal loop
    assert "advisor" not in planner_call["tools"] and "advisor` tool" not in planner_call["text"]


async def test_high_risk_in_auto_requires_approval(tmp_path, monkeypatch):
    steps = [verdict("small", risk="high", needs_plan=True), *PLANNER, *EXECUTOR]
    server = make(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    seen = await run_turn(server, "tidy things", [{"choice": "once"}])
    assert len(seen) == 1 and seen[0]["params"]["tool_name"] == "exit_plan"
    assert seen[0]["params"]["choices"] == ["once", "deny"]
    assert server.session.stored.messages[-1]["content"] == "implemented"
    assert server.session.perms.mode.value == "auto"


async def test_planner_that_produces_no_plan_in_auto_runs_the_task_directly(tmp_path, monkeypatch):
    """A planner that spends its turns reading and never calls exit_plan (nor answers with text) ended the turn with
    "Plan not approved": the task was dropped unexecuted. In auto mode, below high risk, it now runs directly."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    planner_call = {"type": "tool_call", "model": "m-strong", "match": "PLANNING mode", "name": "read"}
    reads = [
        {**planner_call, "when": "first", "arguments": {"path": "a.py"}},
        {**planner_call, "when": "after_tool", "arguments": {"path": "b.py"}},
    ]
    done = {"type": "text", "model": "m-main", "text": "implemented without a plan"}
    server = make(tmp_path, monkeypatch, [verdict("medium"), *reads, done], autonomy={"proposals": False})
    await start(server, tmp_path)
    seen = await run_turn(server, "add a --verbose flag to the CLI")
    assert seen == []  # low risk: nothing to approve
    called = models_called(server)
    assert "m-strong" in called and "m-main" in called, called
    assert server.session.stored.messages[-1]["content"] == "implemented without a plan"
    assert "Plan not approved" not in events(server, "message.complete")[-1]["text"]
    assert [p["status"] for p in events(server, "plan.show")] == []  # no plan was ever proposed
    notes = [n for n in events(server, "notification.show") if n.get("key") == "plan"]
    assert notes and "no plan" in notes[0]["text"]


async def test_planning_turn_that_ends_with_nothing_runs_the_task_directly_in_auto(tmp_path, monkeypatch):
    """The gate's own decision, with the planning turn stubbed to its max-turns outcome (no exit_plan, no text)."""
    from k3code.autonomy.plan_first import PlanFirst, _Captured

    async def nothing(self, session, text, verdict):
        return _Captured()

    monkeypatch.setattr(PlanFirst, "_planning_turn", nothing)
    done = {"type": "text", "model": "m-main", "text": "implemented without a plan"}
    server = make(tmp_path, monkeypatch, [verdict("medium"), done], autonomy={"proposals": False})
    await start(server, tmp_path)
    await run_turn(server, "add a --verbose flag to the CLI")
    assert models_called(server) == ["m-cheap", "m-main"]
    assert server.session.stored.messages[-1]["content"] == "implemented without a plan"
    assert scope_rows(tmp_path)[-1] == {**scope_rows(tmp_path)[-1], "outcome": "done", "planned": False}
    # a refused high-risk plan is still refused (nothing runs): only the no-plan case falls through
    server2 = make(sub(tmp_path, "h"), monkeypatch, [verdict("small", risk="high", needs_plan=True), done])
    await start(server2, tmp_path / "h")
    await run_turn(server2, "tidy things")
    assert "m-main" not in models_called(server2)
    assert "Plan not approved" in events(server2, "message.complete")[-1]["text"]


async def test_high_risk_denied_does_not_execute(tmp_path, monkeypatch):
    steps = [verdict("small", risk="high", needs_plan=True), *PLANNER, *EXECUTOR]
    server = make(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    seen = await run_turn(server, "tidy things", [{"choice": "deny"}])
    assert len(seen) == 1
    assert "m-main" not in models_called(server)  # nothing executed
    assert "Plan not approved" in events(server, "message.complete")[-1]["text"]
    assert [p["status"] for p in events(server, "plan.show")] == ["proposed", "rejected"]
    assert scope_rows(tmp_path)[-1]["outcome"] == "done"  # the turn itself ended cleanly


async def test_danger_prompt_forces_plan_even_if_classifier_says_trivial(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("trivial"), *PLANNER, *EXECUTOR])
    await start(server, tmp_path)
    seen = await run_turn(server, "delete the old migrations folder", [{"choice": "once"}])
    assert len(seen) == 1  # risk=high → approval required
    v = events(server, "scope.verdict")[0]
    assert v["needs_plan"] and v["risk"] == "high"


async def test_trivial_executes_directly_on_the_cheap_tier(tmp_path, monkeypatch):
    """GOAL B5: unimportant work goes to the cheap tier. A trivial interactive task starts there (the live benchmark
    saved only 14 % while half of its tasks, all trivial, ran on main)."""
    cheap_direct = [
        {"type": "text", "model": "m-cheap", "match": "TRIVIAL-TASK", "text": "done directly"},
        usage("m-cheap", "TRIVIAL-TASK", 20, 5),
    ]
    server = make(tmp_path, monkeypatch, [verdict("trivial"), *cheap_direct])
    await start(server, tmp_path)
    await run_turn(server, "TRIVIAL-TASK rename x")
    # classifier, the turn itself (no planning) on the cheap tier, then the cheap post-task proposer
    assert models_called(server) == ["m-cheap", "m-cheap", "m-cheap"]
    assert events(server, "plan.show") == []
    assert server.session.stored.messages[-1]["content"] == "done directly"


async def test_trivial_task_that_stalls_on_the_cheap_tier_escalates_to_main(tmp_path, monkeypatch):
    same = {
        "type": "tool_call",
        "model": "m-cheap",
        "match": "TRIVIAL-TASK",
        "id": "c1",
        "name": "bash",
        "arguments": {"command": "echo hi"},
    }
    steps = [verdict("trivial"), same, {"type": "text", "model": "m-main", "text": "recovered"}]
    server = make(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await run_turn(server, "TRIVIAL-TASK rename x", [{"choice": "once"}] * 5)
    esc = events(server, "routing.escalated")
    assert [(e["from"], e["to"]) for e in esc] == [("cheap", "main")]
    assert models_called(server)[-1] in ("m-main", "m-cheap") and "m-main" in models_called(server)
    assert server.session.stored.messages[-1]["content"] == "recovered"


async def test_cheap_start_turn_escalated_to_main_still_escalates_instead_of_stopping(tmp_path, monkeypatch):
    """The MAIN attempt of a cheap-start turn is followed by STRONG, so its tool-error stop must stay silent."""
    same = {
        "type": "tool_call",
        "model": "m-cheap",
        "match": "TRIVIAL-TASK",
        "id": "c1",
        "name": "bash",
        "arguments": {"command": "echo hi"},
    }
    steps = [verdict("trivial"), same, {"type": "text", "model": "m-main", "text": "recovered"}]
    server = make(tmp_path, monkeypatch, steps)
    builds: list[bool] = []
    real = server._build_loop

    def spy(*args: Any, **kwargs: Any):
        builds.append(kwargs.get("escalates", False))
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "_build_loop", spy)
    await start(server, tmp_path)
    await run_turn(server, "TRIVIAL-TASK rename x", [{"choice": "once"}] * 5)
    assert [(e["from"], e["to"]) for e in events(server, "routing.escalated")] == [("cheap", "main")]
    assert builds[-1] is True, builds


async def test_degrade_trivial_can_be_switched_off(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("trivial"), *DIRECT], autonomy={"degrade_trivial": False})
    await start(server, tmp_path)
    await run_turn(server, "TRIVIAL-TASK rename x")
    assert models_called(server) == ["m-cheap", "m-main", "m-cheap"]  # classifier, the turn on main, proposer


async def test_a_pinned_interactive_tier_is_not_degraded(tmp_path, monkeypatch):
    strong_direct = [
        {"type": "text", "model": "m-strong", "match": "TRIVIAL-TASK", "text": "done directly"},
        usage("m-strong", "TRIVIAL-TASK", 20, 5),
    ]
    server = make(
        tmp_path, monkeypatch, [verdict("trivial"), *strong_direct], task_tiers={"interactive_turn": "strong"}
    )
    await start(server, tmp_path)
    await run_turn(server, "TRIVIAL-TASK rename x")
    assert "m-strong" in models_called(server) and models_called(server)[1] == "m-strong"


async def test_scope_override_forces_plan_for_next_task_only(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("trivial"), *PLANNER, *EXECUTOR, *DIRECT])
    await start(server, tmp_path)
    out = await call(server, "command.dispatch", {"name": "scope", "arg": "large"})
    assert "large" in out["message"]
    await run_turn(server, "rename x")  # trivial text, but overridden to large → plan
    assert [p["status"] for p in events(server, "plan.show")] == ["proposed", "approved"]
    assert events(server, "scope.verdict")[0]["source"] == "override"
    assert events(server, "scope.verdict")[0]["fanout_candidate"] is True
    n = len(events(server, "plan.show"))
    await run_turn(server, "TRIVIAL-TASK again")  # override was consumed → classified again
    assert len(events(server, "plan.show")) == n
    assert events(server, "scope.verdict")[1]["source"] == "classifier"


async def test_default_mode_uses_exit_plan_approval_when_gated(tmp_path, monkeypatch):
    server = make(
        tmp_path,
        monkeypatch,
        [verdict("large"), *PLANNER, *EXECUTOR],
        mode="default",
        autonomy={"gate_modes": ["default", "auto"]},
    )
    await start(server, tmp_path)
    seen = await run_turn(server, "build the feature", [{"choice": "session"}])
    assert len(seen) == 1 and seen[0]["params"]["choices"] == ["once", "session", "deny"]
    assert server.session.perms.mode.value == "accept-edits"  # the existing exit_plan flow switched mode
    assert events(server, "scope.verdict")[0]["fanout_candidate"] is True


async def test_gate_off_in_default_mode_and_when_disabled(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("huge"), *DIRECT], mode="default")
    await start(server, tmp_path)
    await run_turn(server, "TRIVIAL-TASK x")
    assert models_called(server) == ["m-main"] and events(server, "scope.verdict") == []
    server2 = make(sub(tmp_path, "b"), monkeypatch, [verdict("huge"), *DIRECT], autonomy={"plan_first": False})
    await start(server2, tmp_path / "b")
    await run_turn(server2, "TRIVIAL-TASK x")
    assert models_called(server2) == ["m-main"]


async def test_classifier_failure_falls_back_to_direct(tmp_path, monkeypatch):
    # 400 on the cheap tier exhausts that tier's chain → escalates to main; main has no JSON for it → fallback
    steps = [{"type": "error", "model": "m-cheap", "status_code": 400, "message": "nope"}, *DIRECT]
    server = make(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await run_turn(server, "TRIVIAL-TASK x")
    v = events(server, "scope.verdict")[0]
    assert v["source"] == "fallback" and v["scope"] == "small"
    assert server.session.stored.messages[-1]["content"] == "done directly"


# ── escalation ──


async def test_cheap_classification_escalates_to_main_on_chain_exhausted(tmp_path, monkeypatch):
    steps = [
        {"type": "error", "model": "m-cheap", "status_code": 400, "message": "bad model"},
        {**verdict("small"), "model": "m-main"},
        *DIRECT,
    ]
    server = make(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await run_turn(server, "TRIVIAL-TASK x")
    esc = events(server, "routing.escalated")
    assert esc and esc[0]["from"] == "cheap" and esc[0]["to"] == "main" and esc[0]["task_kind"] == "classification"
    assert events(server, "scope.verdict")[0]["source"] == "classifier"  # main answered


async def test_background_turn_escalates_after_repeated_tool_errors(tmp_path, monkeypatch):
    bogus = [
        {"type": "tool_call", "model": "m-cheap", "id": f"c{i}", "name": f"no_such_tool_{i}", "arguments": {}}
        for i in range(3)
    ]
    steps = [*bogus, {"type": "text", "model": "m-main", "text": "recovered"}]
    server = make(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await run_turn(server, "do the thing", background=True)
    esc = events(server, "routing.escalated")
    assert len(esc) == 1 and (esc[0]["from"], esc[0]["to"], esc[0]["reason"]) == ("cheap", "main", "tool_errors")
    called = models_called(server)
    assert called[0] == "m-cheap" and called[-1] == "m-main"
    assert server.session.stored.messages[-1]["content"] == "recovered"
    rows = server.usage.aggregate("session")[0]
    assert rows["by_tier"]["cheap"]["calls"] == 1 and rows["by_tier"]["main"]["calls"] == 1


async def test_background_turn_escalates_when_the_loop_guard_fires(tmp_path, monkeypatch):
    from k3code.reliability import sandbox

    # unattended bash is refused when bwrap is unusable (tool_errors would escalate first); this test is about the guard
    monkeypatch.setattr(sandbox, "should_sandbox", lambda *a, **k: False)
    same = {"type": "tool_call", "model": "m-cheap", "id": "c1", "name": "bash", "arguments": {"command": "echo hi"}}
    steps = [same, {"type": "text", "model": "m-main", "text": "recovered"}]
    server = make(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await run_turn(server, "do the thing", background=True)
    esc = events(server, "routing.escalated")
    assert [(e["from"], e["to"], e["reason"]) for e in esc] == [("cheap", "main", "loop_guard")]
    assert models_called(server)[-1] == "m-main"
    assert server.session.stored.messages[-1]["content"] == "recovered"
    assert server.session.needs_input is False  # the escalated attempt cleared the stop


async def test_background_turn_that_succeeds_stays_on_cheap(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [{"type": "text", "text": "all good"}])
    await start(server, tmp_path)
    await run_turn(server, "ping", background=True)
    assert models_called(server) == ["m-cheap"] and events(server, "routing.escalated") == []


def _bogus(model: str, n: int, start: int = 0) -> list[dict]:
    """``n`` calls of tools that do not exist: each one fails, so ``n`` in a row is a tool-error stall."""
    return [
        {"type": "tool_call", "model": model, "id": f"c{i}", "name": f"no_such_tool_{i}", "arguments": {}}
        for i in range(start, start + n)
    ]


def _stop_messages(server: GatewayServer) -> list[str]:
    return [m["content"] for m in server.session.stored.messages if "tool calls in a row failed" in str(m["content"])]


async def test_main_tier_stall_continues_on_strong(tmp_path, monkeypatch):
    """A turn that starts on main does not stop after 8 failed calls: strong continues it and finishes."""
    steps = [*_bogus("m-main", 8), {"type": "text", "model": "m-strong", "text": "finished on strong"}]
    server = make(tmp_path, monkeypatch, steps, mode="default")
    await start(server, tmp_path)
    await run_turn(server, "do the thing")
    esc = events(server, "routing.escalated")
    assert [(e["from"], e["to"], e["reason"]) for e in esc] == [("main", "strong", "tool_errors")]
    assert models_called(server) == ["m-main", "m-strong"]  # the fake answers all 8 calls in one response
    assert server.session.stored.messages[-1]["content"] == "finished on strong"
    assert _stop_messages(server) == []  # one coherent turn: main's stop message stays silent
    assert events(server, "message.complete")[-1]["status"] == "done"
    assert server.session.needs_input is False
    rows = [(r["kind"], r["detail"]) for r in server.usage.rows() if r["kind"] == "escalated"]
    assert rows == [("escalated", "main->strong: tool_errors")]


async def test_strong_stall_after_main_stops_and_lists_the_failures(tmp_path, monkeypatch):
    steps = [*_bogus("m-main", 8), *_bogus("m-strong", 8, start=8), {"type": "text", "text": "never reached"}]
    server = make(tmp_path, monkeypatch, steps, mode="default")
    await start(server, tmp_path)
    await run_turn(server, "do the thing")
    assert [(e["from"], e["to"]) for e in events(server, "routing.escalated")] == [("main", "strong")]
    (stop,) = _stop_messages(server)
    assert stop.startswith("I stopped because 8 tool calls in a row failed:") and "no_such_tool_15" in stop
    assert events(server, "message.complete")[-1]["status"] == "needs_input"
    assert server.session.needs_input is True


async def test_escalate_main_off_keeps_the_main_tier_stop(tmp_path, monkeypatch):
    steps = [*_bogus("m-main", 8), {"type": "text", "text": "never reached"}]
    server = make(tmp_path, monkeypatch, steps, mode="default", autonomy={"escalate_main": False})
    await start(server, tmp_path)
    await run_turn(server, "do the thing")
    assert events(server, "routing.escalated") == [] and "m-strong" not in models_called(server)
    assert len(_stop_messages(server)) == 1
    assert events(server, "message.complete")[-1]["status"] == "needs_input"


async def test_main_tier_max_turns_cap_ends_needs_input_without_escalating(tmp_path, monkeypatch):
    """The cap is the user's limit on the task, not a stall: strong does not get a fresh round of calls."""
    (tmp_path / "f.txt").write_text("hello\n")
    steps = [
        {"type": "tool_call", "model": "m-main", "id": "r1", "name": "read", "arguments": {"path": "f.txt"}},
        {"type": "text", "model": "m-strong", "text": "strong must not run"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="yolo", max_turns=2)
    await start(server, tmp_path)
    await run_turn(server, "do the thing")
    assert events(server, "routing.escalated") == []
    assert "m-strong" not in models_called(server)
    done = events(server, "message.complete")[-1]
    assert done["status"] == "needs_input" and "I stopped after 2 model calls" in done["text"]
    assert server.session.needs_input is True


async def test_main_tier_loop_guard_stop_continues_on_strong(tmp_path, monkeypatch):
    from k3code.reliability import sandbox

    monkeypatch.setattr(sandbox, "should_sandbox", lambda *a, **k: False)
    same = {"type": "tool_call", "model": "m-main", "id": "c1", "name": "bash", "arguments": {"command": "echo hi"}}
    steps = [same, {"type": "text", "model": "m-strong", "text": "recovered"}]
    server = make(tmp_path, monkeypatch, steps, mode="yolo")
    await start(server, tmp_path)
    await run_turn(server, "do the thing")
    esc = events(server, "routing.escalated")
    assert [(e["from"], e["to"], e["reason"]) for e in esc] == [("main", "strong", "loop_guard")]
    assert server.session.stored.messages[-1]["content"] == "recovered"
    assert server.session.needs_input is False


async def test_interactive_turn_uses_main_and_config_can_override_policy(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [{"type": "text", "text": "hi"}], mode="default")
    await start(server, tmp_path)
    await run_turn(server, "hello")
    assert models_called(server) == ["m-main"]
    server2 = make(
        sub(tmp_path, "x"),
        monkeypatch,
        [{"type": "text", "text": "hi"}],
        mode="default",
        task_tiers={"interactive_turn": "strong"},
    )
    await start(server2, tmp_path / "x")
    await run_turn(server2, "hello")
    assert models_called(server2) == ["m-strong"]


# ── proposals ──


async def test_proposer_emits_dedups_and_accept_sends_action(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("medium"), *PLANNER, *EXECUTOR, *PROPOSER])
    await start(server, tmp_path)
    await run_turn(server, "add a flag")
    shown = events(server, "proposal.show")
    assert len(shown) == 1  # one pass, after the task (none on the plan: a result is what proposals come from)
    assert len([c for p in server.providers for c in p.log if "one step ahead" in c["text"]]) == 1
    assert shown[0]["kind"] == "also_setup" and shown[0]["action"] == "set up CI for this repo"
    lst = await call(server, "command.dispatch", {"name": "proposals", "arg": ""})
    assert shown[0]["id"] in lst["message"]
    acc = await call(server, "command.dispatch", {"name": "proposals", "arg": f"accept {shown[0]['id']}"})
    assert acc["type"] == "send" and acc["message"] == "set up CI for this repo"
    assert "No proposals" in (await call(server, "command.dispatch", {"name": "proposals", "arg": ""}))["message"]


async def test_dismissed_proposal_never_returns(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("medium"), *PLANNER, *EXECUTOR, *PROPOSER])
    await start(server, tmp_path)
    await run_turn(server, "add a flag")
    pid = events(server, "proposal.show")[0]["id"]
    out = await call(server, "command.dispatch", {"name": "proposals", "arg": f"dismiss {pid}"})
    assert "Dismissed" in out["message"]
    await run_turn(server, "add another flag")
    assert len(events(server, "proposal.show")) == 1  # the same suggestion was not re-emitted
    assert "(dismissed)" in (await call(server, "command.dispatch", {"name": "proposals", "arg": "all"}))["message"]
    lines = (k3home(tmp_path) / "proposals.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["status"] == "dismissed"


# ── /preview and /go ──


PREVIEW = [
    {"type": "text", "model": "m-fast", "match": "sketch what the result", "text": "```\n[ todo ]\n```\nRisks:\n- a"}
]


async def test_preview_has_no_tools_runs_on_fast_and_go_executes(tmp_path, monkeypatch):
    steps = [*PREVIEW, {"type": "text", "model": "m-main", "match": "CLI todo", "text": "built it"}]
    server = make(tmp_path, monkeypatch, steps, mode="default")
    await start(server, tmp_path)
    out = await call(server, "command.dispatch", {"name": "preview", "arg": "a CLI todo app"})
    assert "Sketching" in out["message"]  # the ack; the sketch lands when the job ends
    await asyncio.wait_for(server.session.turn_task, 20)
    call_ = server.providers[0].log[0]
    assert call_["model"] == "m-fast" and call_["tools"] == []  # no write tools, no tools at all
    done = events(server, "message.complete")[-1]
    assert "[ todo ]" in done["text"] and "/go" in done["text"]
    assert server.session.stored.meta["preview_task"] == "a CLI todo app"
    go = await call(server, "command.dispatch", {"name": "go", "arg": ""})
    assert go["type"] == "send" and go["message"] == "a CLI todo app"
    await run_turn(server, go["message"])  # what the TUI does with a "send" result
    assert models_called(server) == ["m-fast", "m-main"]
    assert server.session.stored.messages[-1]["content"] == "built it"


async def test_go_without_preview_and_gate_applies_to_go(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [verdict("medium"), *PLANNER, *EXECUTOR, *PREVIEW])
    await start(server, tmp_path)
    assert "Nothing to run" in (await call(server, "command.dispatch", {"name": "go", "arg": ""}))["message"]
    await call(server, "command.dispatch", {"name": "preview", "arg": "add a flag"})
    await asyncio.wait_for(server.session.turn_task, 20)
    go = await call(server, "command.dispatch", {"name": "go", "arg": ""})
    await run_turn(server, go["message"])
    assert events(server, "scope.verdict")  # /go goes through the normal flow, scope gate included


# ── /advisor ──


ADVISOR = [{"type": "text", "model": "m-strong", "match": "senior engineer", "text": "Risk: no tests."}]


async def test_advisor_is_a_side_message_until_accepted(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [*ADVISOR, {"type": "text", "model": "m-main", "text": "ok"}], mode="default")
    await start(server, tmp_path)
    await run_turn(server, "hello")
    before = list(server.session.stored.messages)
    out = await call(server, "command.dispatch", {"name": "advisor", "arg": "is this ok?"})
    assert "Risk: no tests." in out["message"] and out["tag"] == "advisor"
    assert events(server, "advisor.show")[0]["text"] == "Risk: no tests."
    assert server.providers[0].log[-1]["model"] == "m-strong"
    assert server.session.stored.messages == before  # not in the main context
    acc = await call(server, "command.dispatch", {"name": "advisor", "arg": "accept"})
    assert "added" in acc["message"]
    assert "Risk: no tests." in server.session.stored.messages[-1]["content"]


async def test_advisor_compacts_large_conversations_with_cheap_tier(tmp_path, monkeypatch):
    steps = [
        {"type": "text", "model": "m-cheap", "match": "Summarize this coding conversation", "text": "SUMMARY"},
        *ADVISOR,
    ]
    server = make(tmp_path, monkeypatch, steps, mode="default", autonomy={"advisor_compact_chars": 500})
    await start(server, tmp_path)
    live = server.session
    live.messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "x" * 300} for i in range(10)
    ]
    await call(server, "command.dispatch", {"name": "advisor", "arg": "ask"})
    log = server.providers[0].log
    assert [c["model"] for c in log] == ["m-cheap", "m-strong"]
    assert "SUMMARY" in log[1]["text"]


# ── session advisor (Claude Code's advisor mode) ──


TOOL_ADVICE = [{"type": "text", "model": "m-strong", "match": "advisor of a coding agent", "text": "Plan: do X."}]
ASKS_ADVISOR = {
    "type": "tool_call",
    "model": "m-main",
    "when": "first",
    "id": "a1",
    "name": "advisor",
    "arguments": {"question": "How should I structure this?"},
}


async def test_advisor_set_for_session_gives_the_main_model_an_advisor_tool(tmp_path, monkeypatch):
    finish = {"type": "text", "model": "m-main", "when": "after_tool", "text": "followed the advice"}
    server = make(tmp_path, monkeypatch, [*TOOL_ADVICE, ASKS_ADVISOR, finish], mode="default")
    await start(server, tmp_path)
    shown = await call(server, "command.dispatch", {"name": "advisor", "arg": ""})
    assert shown["message"].startswith("Advisor: off")
    out = await call(server, "command.dispatch", {"name": "advisor", "arg": "on"})
    assert "strong tier" in out["message"] and out["advisor"] == "strong"
    assert server.session.stored.meta["advisor"] == "strong"
    await run_turn(server, "build the thing")
    log = server.providers[0].log
    main = [c for c in log if c["model"] == "m-main"]
    assert "advisor" in main[0]["tools"] and "advisor` tool" in main[0]["text"]  # tool + prompt note
    assert [c["model"] for c in log] == ["m-main", "m-strong", "m-main"]
    assert "build the thing" in log[1]["text"] and "How should I structure this?" in log[1]["text"]
    assert any("Plan: do X." in str(m.get("content")) for m in server.session.stored.messages)
    assert server.session.stored.messages[-1]["content"] == "followed the advice"


async def test_advisor_off_removes_the_tool_and_unknown_keys_are_refused(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [{"type": "text", "model": "m-main", "text": "ok"}], mode="default")
    await start(server, tmp_path)
    bad = await call(server, "command.dispatch", {"name": "advisor", "arg": "no-such-model"})
    assert "advisor" not in server.session.stored.meta and "/advisor <model-key>" in bad["message"]
    await call(server, "command.dispatch", {"name": "advisor", "arg": "on"})
    off = await call(server, "command.dispatch", {"name": "advisor", "arg": "off"})
    assert "off" in off["message"] and server.session.stored.meta["advisor"] == ""
    await run_turn(server, "hello")
    main = [c for c in server.providers[0].log if c["model"] == "m-main"]
    assert "advisor" not in main[0]["tools"] and "advisor` tool" not in main[0]["text"]


async def test_advisor_model_key_and_default_persist(tmp_path, monkeypatch):
    from k3code.commands.tune import known_model_keys

    server = make(tmp_path, monkeypatch, [], mode="default")
    await start(server, tmp_path)
    key = sorted(known_model_keys(server.config))[0]
    out = await call(server, "command.dispatch", {"name": "advisor", "arg": f"{key} --default"})
    assert out.get("advisor") == key and "and the default" in out["message"], out
    assert server.session.stored.meta["advisor"] == key
    assert server.session_advisor(server.session) == key


async def test_advisor_auto_hooks_follow_config(tmp_path, monkeypatch):
    # off in config: the plan event carries no critique (large: a scope that gets one when the hook is on)
    server = make(
        tmp_path,
        monkeypatch,
        [verdict("large"), *PLANNER, *ADVISOR_BRIEF, *EXECUTOR],
        autonomy={"advisor_on_plan": False},
    )
    await start(server, tmp_path)
    await run_turn(server, "add a flag")
    assert "advisor" not in events(server, "plan.show")[-1]
    # not in auto mode (and gate forced on via /scope): no critique either
    server2 = make(sub(tmp_path, "d"), monkeypatch, [*PLANNER, *ADVISOR_BRIEF, *EXECUTOR], mode="default")
    assert not server2.autonomy.advisor_applies(_FakeSession("default"), "advisor_on_plan")
    assert server2.autonomy.advisor_applies(_FakeSession("auto"), "advisor_on_plan")
    assert server2.autonomy.advisor_applies(_FakeSession("auto"), "advisor_on_goal")


class _FakeSession:
    def __init__(self, mode: str) -> None:
        from types import SimpleNamespace

        self.perms = SimpleNamespace(mode=SimpleNamespace(value=mode))


async def test_goal_judge_sees_the_failing_test_in_the_evidence_digest(tmp_path, monkeypatch):
    """End to end through the gateway: the executor really runs a failing pytest, and the cheap-tier judge call of
    the implicit goal carries that command and its exit code in the evidence digest."""
    import sys

    from k3code.goals import _EVIDENCE_LABEL
    from k3code.reliability import sandbox

    monkeypatch.setattr(sandbox, "should_sandbox", lambda *a, **k: False)
    (tmp_path / "test_fail.py").write_text("def test_it():\n    assert False\n")
    run_tests = {
        "type": "tool_call",
        "model": "m-main",
        "when": "turn_first",
        "id": "c1",
        "name": "bash",
        "arguments": {"command": f"{sys.executable} -m pytest -q -p no:cacheprovider test_fail.py"},
    }
    claim = {"type": "text", "model": "m-main", "when": "turn_after_tool", "text": "all green, done"}
    judge = {"type": "text", "model": "m-cheap", "match": _EVIDENCE_LABEL, "text": '{"verdict": "done", "reason": "r"}'}
    cfg = {"auto_continue": True, "advisor_on_goal": False, "proposals": False, "degrade_trivial": False}
    server = make(tmp_path, monkeypatch, [verdict("small"), run_tests, claim, judge], autonomy=cfg)
    await start(server, tmp_path)
    await run_turn(server, "make the tests pass")
    judge_calls = [c for p in server.providers for c in p.log if _EVIDENCE_LABEL in c["text"]]
    assert len(judge_calls) == 1 and judge_calls[0]["model"] == "m-cheap"
    digest = judge_calls[0]["text"]
    # the command line is clipped to 160 characters in the digest (the interpreter path is long): pin the outcome
    assert "-> exit 1" in digest and "pytest" in digest
    assert "Last test/check: FAILED, after the last edit." in digest


async def test_review_done_blocks_goal_only_on_blocking_issues(tmp_path, monkeypatch):
    from k3code.autonomy import advisor as adv

    blocking = {
        "type": "text",
        "model": "m-strong",
        "match": "decide whether a goal is really done",
        "text": json.dumps({"blocking": True, "issues": ["tests fail"]}),
    }
    server = make(tmp_path, monkeypatch, [blocking])
    await start(server, tmp_path)
    b, issues = await adv.review_done(server.model_caller, "ship it", "assistant: done")
    assert b is True and issues == ["tests fail"]
    server2 = make(sub(tmp_path, "e"), monkeypatch, [{**blocking, "text": '{"blocking": false, "issues": []}'}])
    await start(server2, tmp_path / "e")
    assert await adv.review_done(server2.model_caller, "ship it", "x") == (False, [])
    # advisor failure never blocks the goal
    server3 = make(sub(tmp_path, "f"), monkeypatch, [{"type": "error", "status_code": 400, "message": "x"}])
    await start(server3, tmp_path / "f")
    assert await adv.review_done(server3.model_caller, "g", "x") == (False, [])


# ── /stats per tier ──


async def test_stats_shows_tokens_and_calls_per_tier(tmp_path, monkeypatch):
    # large: the plan gets the advisor critique, so the strong tier is called twice
    steps = [verdict("large"), usage("m-cheap", "You classify", 11, 3), *PLANNER, *ADVISOR_BRIEF, *EXECUTOR, *PROPOSER]
    server = make(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await run_turn(server, "add a flag")
    out = (await call(server, "command.dispatch", {"name": "stats", "arg": "session"}))["message"]
    assert "tiers:" in out
    for tier in ("cheap", "strong", "main"):
        assert tier in out
    row = server.usage.aggregate("session")[0]
    assert row["by_tier"]["strong"]["calls"] >= 2  # plan + critique
    assert row["by_tier"]["cheap"]["tokens_in"] == 11
    assert {"classification", "plan", "advisor", "interactive_turn"} <= set(row["by_kind"])
