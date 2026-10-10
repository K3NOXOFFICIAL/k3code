"""The per-prompt model-call cap (``max_turns``): none by default, and a configured cap stops loudly (needs_input)."""

from __future__ import annotations

import json

from k3code.config import Settings
from k3code.providers.types import ToolCall
from test_permissions_gateway import call, make_server, run_turn


def _reads(tmp_path, n: int) -> list[ToolCall]:
    for i in range(n):
        (tmp_path / f"f{i}.txt").write_text(f"line {i}\n")
    return [ToolCall(id=f"r{i}", name="read", arguments={"path": f"f{i}.txt"}) for i in range(n)]


def _completed(server) -> dict:
    done = [
        json.loads(f)["params"]["payload"]
        for f in server._frames
        if json.loads(f).get("method") == "event" and json.loads(f)["params"]["type"] == "message.complete"
    ]
    return done[-1]


async def test_a_reached_max_turns_cap_ends_the_turn_as_needs_input_with_a_message(tmp_path, monkeypatch):
    # the model keeps calling tools; before, the cap ended the turn silently as "done"
    server, provider = make_server(tmp_path, _reads(tmp_path, 10), monkeypatch, mode="yolo", max_turns=3)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await run_turn(server, "go", [])
    assert provider.n == 3
    done = _completed(server)
    assert done["status"] == "needs_input" and server.session.needs_input
    assert "I stopped after 3 model calls" in done["text"] and "max_turns" in done["text"]
    last = server.session.stored.messages[-1]
    assert last["role"] == "assistant" and "I stopped after 3 model calls" in last["content"]


async def test_an_active_goal_pauses_at_the_cap_instead_of_being_judged(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, _reads(tmp_path, 10), monkeypatch, mode="yolo", max_turns=2)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    judged: list[str] = []

    async def judge(goal: str, response: str):
        judged.append(response)
        return "continue", "keep going", False, False

    server.goal_judge = judge
    server.goal_manager(server.session).set("finish the work")
    await run_turn(server, "go", [])
    state = server.goal_manager(server.session).state
    assert judged == [] and state.status == "paused" and state.paused_reason == "needs_input"
    assert provider.n == 2


async def test_no_cap_by_default_a_long_task_runs_to_its_answer(tmp_path, monkeypatch):
    assert Settings().max_turns == 0
    server, provider = make_server(tmp_path, [*_reads(tmp_path, 50), "all done"], monkeypatch, mode="yolo")
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await run_turn(server, "go", [])
    assert provider.n == 51
    done = _completed(server)
    assert done["status"] == "done" and done["text"] == "all done" and not server.session.needs_input
    assert sum(1 for m in server.session.stored.messages if m["role"] == "tool") == 50
