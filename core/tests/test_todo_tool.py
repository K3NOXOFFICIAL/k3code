"""The todo tool: the model writes its whole list; at most one item in progress; kept with the session."""

from __future__ import annotations

from k3code.tools import TodoStore, build_registry
from test_autonomy_gateway import events, make, run_turn, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}


async def _call(store: TodoStore, todos) -> dict:
    return await store.tool({"todos": todos})


async def test_write_replaces_the_list_and_renders_it() -> None:
    state: dict = {}
    store = TodoStore(state)
    out = await _call(
        store,
        [
            {"content": "read the code", "status": "completed"},
            {"content": "write the fix", "status": "in_progress"},
            {"content": "run tests", "status": "pending"},
        ],
    )
    assert out["content"] == "Todos (1/3 done):\n[x] read the code\n[>] write the fix\n[ ] run tests"
    assert state["todos"] == [
        {"id": "1", "content": "read the code", "status": "completed"},
        {"id": "2", "content": "write the fix", "status": "in_progress"},
        {"id": "3", "content": "run tests", "status": "pending"},
    ]
    await _call(store, [{"id": "b", "content": "only this", "status": "pending"}])
    assert state["todos"] == [{"id": "b", "content": "only this", "status": "pending"}]  # replaced, not merged
    assert (await _call(store, []))["content"] == "Todo list cleared." and state["todos"] == []


async def test_invalid_lists_are_rejected_and_keep_the_old_one() -> None:
    state = {"todos": [{"id": "1", "content": "keep", "status": "pending"}]}
    store = TodoStore(state)
    two = [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]
    assert "at most one todo may be in_progress" in (await _call(store, two))["error"]
    assert "status 'done'" in (await _call(store, [{"content": "a", "status": "done"}]))["error"]
    assert "no content" in (await _call(store, [{"content": " ", "status": "pending"}]))["error"]
    assert "list" in (await store.tool({"todos": "a, b"}))["error"]
    assert state["todos"] == [{"id": "1", "content": "keep", "status": "pending"}]


def test_registry_offers_the_list_schema() -> None:
    spec, _ = build_registry().get("todo")
    item = spec.parameters["properties"]["todos"]["items"]
    assert item["properties"]["status"]["enum"] == ["pending", "in_progress", "completed"]


async def test_gateway_keeps_the_list_with_the_session_and_sends_it_to_the_tui(tmp_path, monkeypatch) -> None:
    todos = [{"content": "plan", "status": "completed"}, {"content": "build", "status": "in_progress"}]
    steps = [
        {"type": "tool_call", "when": "first", "name": "todo", "arguments": {"todos": todos}},
        {"type": "text", "when": "after_tool", "text": "done"},
    ]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)  # default (ask) mode: todo needs no approval
    await start(server, tmp_path)
    await run_turn(server, "plan it")
    (complete,) = [e for e in events(server, "tool.complete") if e["name"] == "todo"]
    assert complete["todos"] == [
        {"id": "1", "content": "plan", "status": "completed"},
        {"id": "2", "content": "build", "status": "in_progress"},
    ]
    assert "[>] build" in complete["result_text"]
    stored = server.store.get(server.session.session_id)
    assert stored.meta["todos"] == complete["todos"]
