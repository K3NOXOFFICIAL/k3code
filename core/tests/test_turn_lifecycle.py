"""Turn lifecycle regressions: how a turn ends, who may evict or start one, what reaches the disk and when."""

from __future__ import annotations

import asyncio

from k3code.automation.server_runner import ServerRunner
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.router import Router, build_chain
from m1cmd_helpers import git_repo, make_server, new_session

NO_ADVISOR = {"advisor_on_goal": False}


class RepeatingTool:
    """Every model call asks for the same `read`: the loop guard notes it, then stops the turn."""

    name = "fake"
    base_url = "fake://"

    def __init__(self) -> None:
        self.n = 0

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.n += 1
        call = ToolCall(id=f"c{self.n}", name="read", arguments={"path": "a.py"})
        yield StreamEvent(type="done", message=Message(role="assistant", content="", tool_calls=[call]), usage=Usage())

    async def aclose(self) -> None:
        return None


def scripted_judge(verdicts):
    calls: list[tuple[str, str]] = []

    async def judge(goal: str, response: str):
        calls.append((goal, response))
        v = verdicts[min(len(calls) - 1, len(verdicts) - 1)]
        return v, f"verdict {v}", False, False

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def _use(server, provider) -> None:
    server.providers = [provider]
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers = {"default": server.router}  # type: ignore[attr-defined]


async def test_a_loop_guard_stop_pauses_the_goal_instead_of_continuing_it(tmp_path, monkeypatch):
    """The loop-guard stop ended the turn 'done': the judge said continue and the goal re-ran the same tool calls
    turn after turn until the budget ran out."""
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, autonomy=NO_ADVISOR, permission_mode="yolo")
    provider = RepeatingTool()
    _use(server, provider)
    sid = await new_session(server, repo)
    live = server.live[sid]
    mgr = server.goal_manager(live)
    mgr.set("fix the flaky test", max_turns=3)
    server.goal_judge = scripted_judge(["continue"])
    status, _ = await server._run_turn(live, "fix the flaky test")
    assert status == "needs_input"
    assert server.goal_judge.calls == []  # never judged, never continued
    assert mgr.state.status == "paused" and mgr.state.paused_reason == "needs_input"
    assert provider.n <= 5, provider.n  # one guarded turn, not one per goal turn
    await server.close()


async def test_a_loop_guard_stop_reports_a_loop_tick_as_blocked(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, permission_mode="yolo")
    _use(server, RepeatingTool())
    sid = await new_session(server, repo)
    res = await ServerRunner(server).run_prompt("watch the build", session_id=sid, kind="loop_tick")
    assert res.status == "blocked", res
    await server.close()
