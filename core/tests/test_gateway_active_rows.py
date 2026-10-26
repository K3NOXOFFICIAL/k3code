"""The gateway's agent-view data: what state a live session reports, which rows the strip gets (scoped to a project,
finished ones expiring), what each client is pushed, and how ``session.list`` pages and parses ``archived``."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from k3code.gateway import server as server_mod
from k3code.gateway.server import Client, LiveSession
from test_gateway import frames_of, make_server, rpc


def _live(server, cwd: str, *, updated_at: float | None = None, run_result: str | None = None) -> LiveSession:
    stored = server.store.create(model="default", cwd=cwd)
    stored.messages = [{"role": "user", "content": f"work in {cwd}"}]
    if updated_at is not None:
        stored.updated_at = updated_at
    live = LiveSession(stored.session_id, stored, server)
    live.run_result = run_result
    server.live[live.session_id] = live
    return live


# --- LiveSession.state


@pytest.mark.parametrize(
    ("streaming", "needs_input", "open_request", "run_result", "expected"),
    [
        (False, False, False, None, "idle"),
        (True, False, False, None, "working"),
        (True, True, False, None, "needs_input"),  # a question beats "working"
        (True, False, True, None, "needs_input"),  # so does an open approval/clarify request
        (False, False, True, "completed", "needs_input"),
        (False, False, False, "completed", "completed"),
        (False, False, False, "failed", "failed"),
        (True, False, False, "completed", "working"),  # the next turn started
    ],
)
def test_live_session_state_matrix(streaming, needs_input, open_request, run_result, expected) -> None:
    server = make_server()
    live = _live(server, "/proj", run_result=run_result)
    live.streaming, live.needs_input = streaming, needs_input
    server.has_open_request = lambda sid: open_request  # type: ignore[method-assign]
    assert live.state == expected


# --- _active_rows: project scope and finished-row expiry


def test_rows_are_limited_to_the_asked_project(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "a")
    server = make_server()
    in_a = _live(server, str(tmp_path / "a"))
    via_link = _live(server, str(link))  # the same directory spelled through a symlink
    _live(server, str(tmp_path / "b"))
    ids = {r["id"] for r in server._active_rows(None, str((tmp_path / "a").resolve()))}
    assert ids == {in_a.session_id, via_link.session_id}
    assert len(server._active_rows(None)) == 3  # no cwd: every project, as before


def test_a_subagent_follows_its_parents_project(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    server = make_server()
    parent_a = _live(server, str(tmp_path / "a"))
    parent_b = _live(server, str(tmp_path / "b"))

    def handle(hid: str, parent: str):
        return SimpleNamespace(
            id=hid, status="running", started_wall=1.0, tool_count=0, model="m", tier="cheap",
            description="d", parent_sid=parent,
        )  # fmt: skip

    server.subagents.handles = {"h1": handle("h1", parent_a.session_id), "h2": handle("h2", parent_b.session_id)}
    ids = {r["id"] for r in server._active_rows(None, str((tmp_path / "a").resolve()))}
    assert ids == {parent_a.session_id, "h1"}


def test_finished_sessions_expire_but_unfinished_ones_never_do() -> None:
    server = make_server()
    now = 10_000.0
    old = server_mod._COMPLETED_LIVE_TTL_S + 1
    stale_done = _live(server, "/p", updated_at=now - old, run_result="completed")
    stale_failed = _live(server, "/p", updated_at=now - old, run_result="failed")
    fresh_done = _live(server, "/p", updated_at=now - 5, run_result="completed")
    stale_idle = _live(server, "/p", updated_at=now - old)  # never ran a turn: not "finished"
    stale_working = _live(server, "/p", updated_at=now - old)
    stale_working.streaming = True
    ids = {r["id"] for r in server._active_rows(None, now=now)}
    assert ids == {fresh_done.session_id, stale_idle.session_id, stale_working.session_id}
    assert stale_done.session_id not in ids and stale_failed.session_id not in ids
    assert stale_done.session_id in server.live  # dropped from the strip only, still live


def test_the_asking_clients_own_session_never_expires_from_its_list() -> None:
    """`run_result` stays "completed" after every normal turn: 30 minutes after the last one the client's own session
    would drop out of its own list (no current row: the title bar goes blank, the view loses it)."""
    server = make_server()
    now = 10_000.0
    mine = _live(server, "/p", updated_at=now - server_mod._COMPLETED_LIVE_TTL_S - 1, run_result="completed")
    other = _live(server, "/p", updated_at=now - server_mod._COMPLETED_LIVE_TTL_S - 1, run_result="completed")
    rows = {r["id"]: r for r in server._active_rows(mine.session_id, now=now)}
    assert set(rows) == {mine.session_id} and rows[mine.session_id]["current"]
    assert other.session_id not in rows


def test_only_the_newest_finished_sessions_stay_listed(monkeypatch) -> None:
    monkeypatch.setattr(server_mod, "_COMPLETED_LIVE_MAX", 3)
    server = make_server()
    now = time.time()
    finished = [_live(server, "/p", updated_at=now - i, run_result="completed") for i in range(6)]
    working = _live(server, "/p", updated_at=now - 100)
    working.streaming = True
    ids = [r["id"] for r in server._active_rows(None, now=now)]
    assert ids == [f.session_id for f in finished[:3]] + [working.session_id]  # newest first; the capped ones left
    assert working.session_id in ids  # the cap counts finished rows only


# --- pushed snapshots


def _attach(server, session_id: str | None = None, cwd: str | None = None) -> tuple[Client, list[str]]:
    sent: list[str] = []
    client = Client(sent.append, name=f"c{len(server.clients)}", authenticated=True)
    client.session_id, client.cwd = session_id, cwd
    server.clients.append(client)
    return client, sent


def _pushed(sent: list[str]) -> list[dict]:
    frames = [json.loads(line) for line in sent]
    return [f["params"]["payload"] for f in frames if f["params"]["type"] == "session.active_list"]


def test_every_client_is_pushed_its_own_project_and_current_mark(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    server = make_server()
    a = _live(server, str(tmp_path / "a"))
    b = _live(server, str(tmp_path / "b"))
    _, sent_a = _attach(server, a.session_id)
    _, sent_b = _attach(server, b.session_id)
    _, sent_none = _attach(server, None)  # no session and no asked cwd: unscoped
    server.broadcast_active_list()
    (snap_a,), (snap_b,), (snap_none,) = _pushed(sent_a), _pushed(sent_b), _pushed(sent_none)
    assert [r["id"] for r in snap_a["sessions"]] == [a.session_id]
    assert [(r["id"], r["current"]) for r in snap_b["sessions"]] == [(b.session_id, True)]
    assert {r["id"] for r in snap_none["sessions"]} == {a.session_id, b.session_id}
    assert not any(r["current"] for r in snap_none["sessions"])


async def test_the_cwd_a_client_polls_with_also_scopes_its_pushes(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    server = make_server()
    a = _live(server, str(tmp_path / "a"))
    _live(server, str(tmp_path / "b"))
    res = await rpc(server, "session.active_list", {"cwd": str(tmp_path / "a")})
    assert [r["id"] for r in res["result"]["sessions"]] == [a.session_id]
    assert server._stdio_client.cwd == str((tmp_path / "a").resolve())
    server.broadcast_active_list()
    pushed = [f["params"]["payload"] for f in frames_of(server) if f.get("method") == "event"]
    assert [[r["id"] for r in p["sessions"]] for p in pushed] == [[a.session_id]]


async def test_active_list_rejects_a_cwd_that_is_not_a_string() -> None:
    server = make_server()
    res = await rpc(server, "session.active_list", {"cwd": 5})
    assert "error" in res


# --- session.list: archived parsing, total and offset


def _stored(server, cwd: str, *, updated_at: float, archived: bool = False) -> str:
    s = server.store.create(cwd=cwd)
    s.messages = [{"role": "user", "content": "q"}]
    server.store.save(s)
    server.store._db.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (updated_at, s.session_id))
    server.store._db.commit()
    if archived:
        server.store.set_archived([s.session_id], time.time())
    return s.session_id


@pytest.mark.parametrize(
    ("value", "lists_archived"),
    [(False, False), ("false", False), ("False", False), (0, False), (None, False)]
    + [(True, True), ("true", True), ("TRUE", True), (1, True)],
)
async def test_archived_is_parsed_as_a_boolean_not_as_truthiness(value, lists_archived) -> None:
    server = make_server()
    kept = _stored(server, "/p", updated_at=2)
    gone = _stored(server, "/p", updated_at=1, archived=True)
    res = await rpc(server, "session.list", {"archived": value, "limit": 10})
    assert [r["id"] for r in res["result"]["sessions"]] == [gone if lists_archived else kept]


@pytest.mark.parametrize("params", [{"offset": "x"}, {"limit": "many"}, {"offset": [1]}])
async def test_a_page_size_or_offset_that_is_not_a_number_is_an_invalid_param(params) -> None:
    res = await rpc(make_server(), "session.list", params)
    assert "error" in res and res["error"]["code"] == -32602


@pytest.mark.parametrize("value", ["yes", 2, [], {}, 1.5])
async def test_archived_with_any_other_value_is_an_invalid_param(value) -> None:
    server = make_server()
    res = await rpc(server, "session.list", {"archived": value})
    assert "error" in res


async def test_session_list_reports_the_total_and_pages_by_offset_without_cwd() -> None:
    server = make_server()
    ids = [_stored(server, f"/p{i}", updated_at=100 - i) for i in range(7)]
    _stored(server, "/p", updated_at=1, archived=True)  # not part of the default total
    first = (await rpc(server, "session.list", {"limit": 3}))["result"]
    assert first["total"] == 7 and first["offset"] == 0
    assert [r["id"] for r in first["sessions"]] == ids[:3]
    last = (await rpc(server, "session.list", {"limit": 3, "offset": 6}))["result"]
    assert last["total"] == 7 and last["offset"] == 6
    assert [r["id"] for r in last["sessions"]] == ids[6:]


async def test_session_list_reports_the_total_and_pages_by_offset_with_cwd(tmp_path) -> None:
    (tmp_path / "p").mkdir()
    server = make_server()
    mine = [_stored(server, str(tmp_path / "p"), updated_at=100 - i) for i in range(5)]
    for i in range(4):
        _stored(server, "/elsewhere", updated_at=50 - i)
    _stored(server, str(tmp_path / "p"), updated_at=1, archived=True)
    params = {"cwd": str(tmp_path / "p"), "limit": 2}
    page1 = (await rpc(server, "session.list", params))["result"]
    page2 = (await rpc(server, "session.list", {**params, "offset": 2}))["result"]
    page3 = (await rpc(server, "session.list", {**params, "offset": 4}))["result"]
    assert [p["total"] for p in (page1, page2, page3)] == [5, 5, 5]  # the full count, not just the page
    assert [r["id"] for p in (page1, page2, page3) for r in p["sessions"]] == mine
    archived = (await rpc(server, "session.list", {**params, "archived": "true"}))["result"]
    assert archived["total"] == 1
