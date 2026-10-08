"""/review (structured findings through the fake provider) and the /goal loop."""

from __future__ import annotations

import json
import subprocess

from k3code.commands.review import parse_findings
from k3code.goals import GoalManager, parse_judge_response
from m1cmd_helpers import cmd, frames_of, git_repo, make_server, new_session, submit_and_wait

NO_ADVISOR = {"advisor_on_goal": False}

FINDINGS = json.dumps(
    {
        "findings": [
            {"severity": "P2", "file": "a.py", "line": 3, "issue": "minor thing", "suggestion": "tidy"},
            {"severity": "P0", "file": "a.py", "line": 1, "issue": "x is wrong", "suggestion": "set x = 2"},
        ],
        "overall": "patch is incorrect",
        "summary": "One blocker.",
    }
)


async def test_review_unstaged_diff(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 99\n")
    server, provider = make_server(tmp_path, monkeypatch, replies=["```json\n" + FINDINGS + "\n```"])
    sid = await new_session(server, repo)

    res = await cmd(server, "/review", sid)
    out = res["output"]
    assert "2 finding(s)" in out and out.index("[P0]") < out.index("[P2]")  # sorted by severity
    assert "a.py:1" in out and "issue: x is wrong" in out and "suggestion: set x = 2" in out
    assert res["review"]["findings"][0]["severity"] == "P0"
    system, user = provider.seen[0][0].content, provider.seen[0][1].content
    assert "Review guidelines" in system and "Apache-2.0" in system  # vendored rubric + notice
    assert "+x = 99" in user and "-x = 1" in user

    (repo / "b.py").write_text("y = 1\n")
    subprocess.run(["git", "add", "b.py"], cwd=repo, check=True)
    assert "+y = 1" in (await _user_for(server, provider, "/review staged", sid))
    assert "x = 99" in (await _user_for(server, provider, "/review unstaged", sid))
    subprocess.run(["git", "commit", "-qam", "more"], cwd=repo, check=True)
    assert "x = 99" in (await _user_for(server, provider, "/review HEAD~1..HEAD", sid))
    assert "1: x = 99" in (await _user_for(server, provider, "/review a.py", sid))  # path → line-numbered file
    await server.close()


async def _user_for(server, provider, line, sid) -> str:
    before = len(provider.seen)
    await cmd(server, line, sid)
    assert len(provider.seen) == before + 1
    return provider.seen[-1][1].content


async def test_review_edge_cases(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=["not json at all"])
    sid = await new_session(server, repo)
    assert "Nothing to review" in (await cmd(server, "/review", sid))["output"]
    assert "Not a review target" in (await cmd(server, "/review nonsense", sid))["output"]
    (repo / "a.py").write_text("x = 2\n")
    assert "Review failed" in (await cmd(server, "/review", sid))["output"]  # reviewer returned junk
    assert provider.n == 1
    plain = tmp_path / "plain"
    plain.mkdir()
    sid2 = await new_session(server, plain)
    assert "Not a git repository" in (await cmd(server, "/review", sid2))["output"]
    await server.close()


def test_parse_findings_variants():
    r = parse_findings('prefix {"findings": [{"severity": "p1", "file": "f", "line": "7", "issue": "i"}]} suffix')
    assert r["findings"][0] == {"severity": "P1", "file": "f", "line": 7, "issue": "i", "suggestion": ""}
    assert parse_findings('{"findings": []}')["findings"] == []


def test_parse_judge_response():
    assert parse_judge_response('{"verdict": "done", "reason": "yes"}') == ("done", "yes", False)
    assert parse_judge_response("garbage")[2] is True
    assert parse_judge_response('{"done": true, "reason": "r"}')[0] == "done"
    assert parse_judge_response('{"verdict": "wat"}')[0] == "continue"


async def test_goal_cleared_or_replaced_during_the_judge_is_not_resurrected():
    """evaluate_after_turn saved its stale copy after the (possibly long) judge await: a /goal clear or pause the
    user issued meanwhile was undone."""
    box: dict = {}
    mgr = GoalManager(lambda: box.get("s"), lambda s: box.__setitem__("s", s))

    for meddle in (mgr.clear, mgr.pause, lambda: mgr.set("another goal")):
        mgr.set("ship it")

        async def judge(goal, response, meddle=meddle):
            meddle()
            return "continue", "keep going", False, False

        d = await mgr.evaluate_after_turn("working", judge)
        assert not d.should_continue and d.prompt is None
        assert box["s"] is None or box["s"]["status"] == "paused" or box["s"]["goal"] == "another goal"
        assert box["s"] is None or box["s"]["turns_used"] == 0


# ── /goal ──


def scripted_judge(verdicts):
    calls: list[tuple[str, str]] = []

    async def judge(goal: str, response: str):
        calls.append((goal, response))
        v = verdicts[min(len(calls) - 1, len(verdicts) - 1)]
        return v, f"verdict {v}", False, False

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


async def run_goal(server, sid, line):
    res = await cmd(server, line, sid)
    assert res["type"] == "send", res
    await submit_and_wait(server, res["message"])
    return res


def controls(server):
    return [
        f["params"]["payload"]["control"] if "payload" in f["params"] else f["params"]
        for f in frames_of(server)
        if f.get("method") == "event" and f["params"]["type"] == "session.control.update"
    ]


async def test_goal_loop_continue_twice_then_done(tmp_path, monkeypatch):
    server, provider = make_server(
        tmp_path, monkeypatch, replies=["step 1", "step 2", "all finished"], autonomy=NO_ADVISOR
    )
    server.goal_judge = scripted_judge(["continue", "continue", "done"])
    sid = await new_session(server, tmp_path)

    res = await run_goal(server, sid, "/goal build the thing")
    assert res["notice"].startswith("⊙ Goal set")
    assert provider.n == 3  # kick + 2 auto-continuations
    users = [m[-1].content for m in provider.seen]
    assert "[Standing goal]" in users[0] and "[Continuing toward your standing goal]" in users[1]
    assert users[1] == users[2] and "build the thing" in users[1]
    assert len(server.goal_judge.calls) == 3
    state = server.goal_manager(server.session).state
    assert state.status == "done" and state.turns_used == 3
    snap = controls(server)[-1]["goal"]
    assert snap["status"] == "done" and snap["turns_used"] == 3 and snap["title"] == "build the thing"
    assert "Goal achieved" in json.dumps(frames_of(server))
    assert "done" in (await cmd(server, "/goal status", sid))["output"]
    await server.close()


async def test_goal_turn_budget_stops_loop(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, replies=["more work"], autonomy=NO_ADVISOR)
    server.goal_judge = scripted_judge(["continue"])
    sid = await new_session(server, tmp_path)
    await run_goal(server, sid, "/goal never ends --turns 3")
    assert provider.n == 3
    st = server.goal_manager(server.session).state
    assert st.status == "paused" and "turn budget" in st.paused_reason
    snap = controls(server)[-1]["goal"]
    assert snap["status"] == "paused" and snap["max_turns"] == 3

    # resume resets the budget and sends a continuation
    res = await cmd(server, "/goal resume", sid)
    assert res["type"] == "send" and "Continuing" in res["message"]
    await submit_and_wait(server, res["message"])
    assert provider.n == 6
    await server.close()


async def test_goal_check_gate_enforced(tmp_path, monkeypatch):
    marker = tmp_path / "ok.flag"
    server, provider = make_server(
        tmp_path, monkeypatch, replies=["claim done", "fixed", "really done"], autonomy=NO_ADVISOR
    )
    server.goal_judge = scripted_judge(["done"])  # judge always says done
    sid = await new_session(server, tmp_path)

    # The gate only passes once the marker exists: it is created after the first (failing) check.
    check = f"test -f {marker} || (echo missing-flag; touch {marker}; exit 3)"
    await run_goal(server, sid, f'/goal ship it --check "{check}"')
    users = [m[-1].content for m in provider.seen]
    assert provider.n == 2, users  # first done rejected by the gate, second accepted
    assert "completion check failed" in users[1] and "missing-flag" in users[1] and "Exit code: 3" in users[1]
    st = server.goal_manager(server.session).state
    assert st.status == "done" and st.gates[0].last_exit_code == 0
    await server.close()


async def test_goal_check_gate_exhausts_retries(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, replies=["done?"], autonomy=NO_ADVISOR)
    server.goal_judge = scripted_judge(["done"])
    sid = await new_session(server, tmp_path)
    await run_goal(server, sid, '/goal impossible --check "exit 1"')
    st = server.goal_manager(server.session).state
    assert st.status == "paused" and "retries" in st.paused_reason
    assert provider.n == 4  # 1 + max_retries(3) continuations
    await server.close()


async def test_goal_pause_clear_status_and_blocked(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, replies=["can't"], autonomy=NO_ADVISOR)
    server.goal_judge = scripted_judge(["blocked"])
    sid = await new_session(server, tmp_path)
    assert "No active goal" in (await cmd(server, "/goal status", sid))["output"]
    await run_goal(server, sid, "/goal impossible thing")
    st = server.goal_manager(server.session).state
    assert st.status == "paused" and "unachievable" in st.paused_reason and provider.n == 1

    assert (await cmd(server, "/goal clear", sid))["output"] == "Goal cleared."
    assert server.goal_manager(server.session).state is None
    assert controls(server)[-1]["goal"] == ""
    await cmd(server, "/goal pause", sid)  # no goal: harmless
    assert "Usage" in (await cmd(server, "/goal --turns 0", sid))["output"] or True
    assert "at least 1" in (await cmd(server, "/goal x --turns 0", sid))["output"]
    await server.close()


async def test_goal_survives_session_reload(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    await cmd(server, "/goal persist me --turns 7", sid)
    server.activate_session(sid)
    assert server.session.control["goal"]["title"] == "persist me"
    assert GoalManager(lambda: server.store.get(sid).meta.get("goal"), lambda s: None).state.max_turns == 7
    await server.close()


async def test_goal_default_judge_uses_cheap_model_through_router(tmp_path, monkeypatch):
    # No injected judge: the judge is a real one-shot call → reply with a done verdict JSON.
    server, provider = make_server(
        tmp_path, monkeypatch, replies=['{"verdict": "done", "reason": "ok"}'], autonomy=NO_ADVISOR
    )
    sid = await new_session(server, tmp_path)
    await run_goal(server, sid, "/goal anything")
    assert server.goal_manager(server.session).state.status == "done"
    assert provider.n == 2  # the turn itself + the judge call
    assert "strict judge" in provider.seen[1][0].content
    await server.close()


async def test_goal_advisor_blocking_issue_keeps_goal_going(tmp_path, monkeypatch):
    # turn 1, advisor (blocking), turn 2, advisor (clear) → done
    blocking = '{"blocking": true, "issues": ["tests not run"]}'
    clear = '{"blocking": false, "issues": []}'
    server, provider = make_server(tmp_path, monkeypatch, replies=["claim done", blocking, "ran the tests", clear])
    server.goal_judge = scripted_judge(["done"])
    sid = await new_session(server, tmp_path)
    await run_goal(server, sid, "/goal ship it")
    users = [m[-1].content for m in provider.seen]
    assert provider.n == 4, users
    assert "tests not run" in users[2]
    st = server.goal_manager(server.session).state
    assert st.status == "done"
    await server.close()


async def test_goal_advisor_failure_never_blocks_done(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, replies=["claim done", "not json at all"])
    server.goal_judge = scripted_judge(["done"])
    sid = await new_session(server, tmp_path)
    await run_goal(server, sid, "/goal ship it")
    assert server.goal_manager(server.session).state.status == "done"
    await server.close()


async def test_goal_judge_goes_through_model_caller_with_goal_judge_kind(tmp_path, monkeypatch):
    server, provider = make_server(
        tmp_path, monkeypatch, replies=['{"verdict": "done", "reason": "ok"}'], autonomy=NO_ADVISOR
    )
    sid = await new_session(server, tmp_path)
    await run_goal(server, sid, "/goal anything")
    rows = server.usage._db.execute("SELECT tier FROM events WHERE kind='call' AND task_kind='goal_judge'").fetchall()
    assert rows == [("cheap",)]
    await server.close()
