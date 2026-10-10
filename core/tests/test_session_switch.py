"""Session switching: needs-input state while an approval is open, session.close, transcript rows on resume."""

from __future__ import annotations

import asyncio
import time

import pytest

import m1cmd_helpers as m1
from k3code.gateway.server import transcript_rows
from k3code.gateway.sessions import StoredSession
from k3code.router import Router, build_chain
from test_permissions_gateway import call, make_server

DAY = 24 * 3600


async def test_open_approval_makes_the_session_need_input(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    sid = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    live = server.live[sid]
    assert live.state == "idle"
    server._open_requests["approval-1"] = (sid, "{}")
    assert live.state == "needs_input" and server.has_open_request(sid)
    row = next(r for r in (await call(server, "session.active_list", {}))["sessions"] if r["id"] == sid)
    assert row["state"] == "needs_input" and row["status"] == "needs_input"
    server._open_requests.clear()
    assert live.state == "idle"


async def test_session_close_drops_idle_sessions_but_keeps_busy_ones(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    b = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]  # the client moves to b
    server._open_requests["approval-1"] = (a, "{}")
    assert (await call(server, "session.close", {"session_id": a}))["closed"] is False  # waiting for an answer
    server._open_requests.clear()
    assert (await call(server, "session.close", {"session_id": a}))["closed"] is True
    assert a not in server.live and server.store.get(a) is not None  # still resumable
    assert (await call(server, "session.close", {"session_id": b}))["closed"] is False  # the client is on it
    assert (await call(server, "session.close", {"session_id": "nope"}))["closed"] is False


def _worked_in(server, cwd: str) -> str:
    s = server.store.create(cwd=cwd)
    s.messages = [{"role": "user", "content": "hi"}]
    server.store.save(s)
    return s.session_id


async def test_most_recent_skips_sessions_with_no_message(tmp_path, monkeypatch):
    """With tui_auto_resume_recent a plain `k3code` resumes session.most_recent: an empty session left newest (a TUI
    start that never got a prompt) opened an empty chat instead of the user's last work."""
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    older = _worked_in(server, str(tmp_path))
    server.store.create(cwd=str(tmp_path))  # newer, empty
    assert server.store.most_recent().session_id == older
    assert (await call(server, "session.most_recent", {}))["session_id"] == older
    await server.close()


async def test_most_recent_on_a_store_of_only_empty_sessions_is_like_an_empty_store(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    assert await call(server, "session.most_recent", {}) == {"session_id": None}
    server.store.create(cwd=str(tmp_path))
    server.store.create(cwd=str(tmp_path))
    assert server.store.most_recent() is None
    assert await call(server, "session.most_recent", {}) == {"session_id": None}
    await server.close()


async def test_disposable_only_close_drops_an_empty_session_the_caller_left(tmp_path, monkeypatch):
    """The TUI sends session.close {disposable_only} for the session it just switched away from: an empty idle one
    leaves the live registry; its stored row (settings such as model or workspace) stays."""
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    res = await call(server, "session.close", {"session_id": a, "disposable_only": True})
    assert res == {"closed": False, "reason": "still in use"}  # the caller has not switched away yet
    assert a in server.live and server.store.get(a) is not None
    await call(server, "session.create", {"cwd": str(tmp_path)})  # the caller moves on
    assert (await call(server, "session.close", {"session_id": a, "disposable_only": True}))["closed"] is True
    assert a not in server.live and server.store.get(a) is not None
    await server.close()


async def test_disposable_only_close_keeps_the_row_and_most_recent_still_skips_it(tmp_path, monkeypatch):
    """The kept empty row is newer than the user's last work, yet auto-resume still lands on that work."""
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    older = _worked_in(server, str(tmp_path))
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    await call(server, "session.create", {"cwd": str(tmp_path)})  # the caller moves on
    assert (await call(server, "session.close", {"session_id": a, "disposable_only": True}))["closed"] is True
    assert server.store.get(a) is not None
    assert (await call(server, "session.most_recent", {}))["session_id"] == older
    await server.close()


@pytest.mark.parametrize("binding", ["loop", "automation prompt", "automation trigger"])
async def test_disposable_only_close_keeps_an_empty_session_an_automation_is_bound_to(tmp_path, monkeypatch, binding):
    """An empty session owning `/loop daily 09:00 ...` must stay: the loop ticks into it."""
    from k3code.automation.engine import AutomationEngine

    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    server.automation = AutomationEngine(server, use_netwatch=False)
    await server.automation.start()
    if binding == "loop":
        server.automation.loops.create(session_id=a, prompt="check the build", interval="daily 09:00")
    else:
        prompt = {"type": "prompt", "prompt": "summarise", "session": a if binding == "automation prompt" else ""}
        trigger = {"type": "session_event", "event": "completed"}
        if binding == "automation trigger":
            trigger["session"] = a
        server.automation.db.insert("automations", id="au1", name="x", trigger=trigger, action=prompt, created_at=0.0)
    await call(server, "session.create", {"cwd": str(tmp_path)})  # the caller moves on
    res = await call(server, "session.close", {"session_id": a, "disposable_only": True})
    assert res == {"closed": False, "reason": "not disposable"}
    assert a in server.live and server.store.get(a) is not None
    if binding == "loop":
        server.automation.loops.stop(session_id=a)
    else:
        server.automation.db.update("automations", "au1", state="paused")
    assert (await call(server, "session.close", {"session_id": a, "disposable_only": True}))["closed"] is True
    await server.close()


@pytest.mark.parametrize(
    ("busy", "reason"), [("message", "not disposable"), ("working", "still in use"), ("background", "still in use")]
)
async def test_disposable_only_close_leaves_a_session_with_content_or_work(tmp_path, monkeypatch, busy, reason):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    live = server.live[a]
    if busy == "message":
        live.stored.messages.append({"role": "user", "content": "hi"})
        server.store.save(live.stored)
    elif busy == "working":
        live.streaming = True
    else:
        live.background = True
    await call(server, "session.create", {"cwd": str(tmp_path)})  # the caller moves on
    res = await call(server, "session.close", {"session_id": a, "disposable_only": True})
    assert res == {"closed": False, "reason": reason}
    assert a in server.live and server.store.get(a) is not None
    live.streaming = False
    live.background = False
    await server.close()


async def test_default_close_keeps_the_stored_row_of_an_empty_session(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    a = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    await call(server, "session.create", {"cwd": str(tmp_path)})
    assert (await call(server, "session.close", {"session_id": a}))["closed"] is True
    assert a not in server.live and server.store.get(a) is not None
    await server.close()


def test_transcript_rows_shape():
    rows = transcript_rows(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "bash", "arguments": "ls"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
            {"role": "assistant", "content": "done"},
        ]
    )
    assert rows == [
        {"role": "user", "text": "hi"},
        {"role": "tool", "name": "bash", "context": "ls"},
        {"role": "assistant", "text": "done"},
    ]


class _FlakyProvider(m1.TextProvider):
    """Plays the scripted replies; while ``fail`` is set every stream raises, so the turn ends with status "error"."""

    fail = False

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        if self.fail:
            raise RuntimeError("provider exploded")
        async for event in super().stream(messages, tools, model, max_tokens=max_tokens, temperature=temperature):
            yield event


async def test_a_foreground_turn_reports_completed_and_failed_until_the_next_prompt(tmp_path, monkeypatch):
    """The agent view and the strip show the last turn's outcome: a foreground turn that ended in an error used to
    read 'idle', like one that finished, so it could never show as failed."""
    server, _ = m1.make_server(tmp_path, monkeypatch, ["done"])
    provider = _FlakyProvider(["done"])
    server.providers = [provider]  # type: ignore[list-item]
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)  # type: ignore[list-item]
    server._oneshot_routers["default"] = server.router  # type: ignore[attr-defined]
    sid = await m1.new_session(server, tmp_path)
    live = server.live[sid]
    assert not live.background
    await m1.submit_and_wait(server, "hi")
    assert provider.n == 1 and live.state == "completed"
    row = next(r for r in (await m1.rpc(server, "session.active_list", {}))["result"]["sessions"] if r["id"] == sid)
    assert row["state"] == "completed"

    provider.fail = True
    await m1.submit_and_wait(server, "again")
    assert live.state == "failed"
    events = [f["params"] for f in m1.frames_of(server) if f.get("method") == "event"]
    done = [e["payload"] for e in events if e.get("type") == "message.complete"]
    assert done[-1]["status"] == "error" and done[-1]["state"] == "failed"
    row = next(r for r in (await m1.rpc(server, "session.active_list", {}))["result"]["sessions"] if r["id"] == sid)
    assert row["state"] == "failed"

    # cleared when the next turn starts, exactly like a background run's result
    provider.fail = False
    seen: list[str | None] = []
    original = server.autonomy.prepare

    async def prepare(session, text):
        seen.append(session.run_result)  # inside the turn, after the reset
        return await original(session, text)

    server.autonomy.prepare = prepare  # type: ignore[method-assign]
    await m1.submit_and_wait(server, "third")
    assert seen == [None]
    assert live.run_result == "completed" and live.state == "completed"


async def test_a_job_reports_its_own_outcome_not_an_earlier_turns_failure(tmp_path, monkeypatch):
    """/ultraplan, /ultracode and /ultraresearch run through start_job: a 'failed' left by an earlier turn must not
    reach the job's closing events, and a job's own result (completed or failed) replaces it."""
    server, _ = m1.make_server(tmp_path, monkeypatch, ["done"])
    provider = _FlakyProvider(["done"])
    server.providers = [provider]  # type: ignore[list-item]
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)  # type: ignore[list-item]
    server._oneshot_routers["default"] = server.router  # type: ignore[attr-defined]
    sid = await m1.new_session(server, tmp_path)
    live = server.live[sid]

    provider.fail = True
    await m1.submit_and_wait(server, "boom")
    assert live.state == "failed"

    def closing(since: int) -> tuple[dict, dict]:
        events = [f["params"] for f in m1.frames_of(server)[since:] if f.get("method") == "event"]
        done = [e["payload"] for e in events if e.get("type") == "message.complete"][-1]
        status = [e["payload"] for e in events if e.get("type") == "status.update"][-1]
        return done, status

    async def ok_job() -> str:
        return "job result"

    async def bad_job() -> str:
        raise RuntimeError("job exploded")

    seen: list[str | None] = []

    async def probe_job() -> str:
        seen.append(live.run_result)  # inside the job, after the reset
        return "probe"

    n = len(m1.frames_of(server))
    server.start_job(live, "ultraplan x", ok_job)
    await live.turn_task
    done, status = closing(n)
    assert done["status"] == "done" and done["state"] == "completed" and status["state"] == "completed"
    assert live.run_result == "completed"

    n = len(m1.frames_of(server))
    server.start_job(live, "ultracode y", bad_job)
    await live.turn_task
    done, status = closing(n)
    assert done["status"] == "error" and done["state"] == "failed" and status["state"] == "failed"
    assert live.run_result == "failed"

    server.start_job(live, "ultraresearch z", probe_job)
    await live.turn_task
    assert seen == [None] and live.state == "completed"


async def test_an_interrupted_foreground_turn_reports_completed_until_the_next_prompt(tmp_path, monkeypatch):
    """/stop and Esc cancel the turn task; the CancelledError skipped the outcome mapping, so an interrupted turn read
    'idle' in the agent view and the strip (and kept an earlier turn's 'failed')."""
    server, _ = m1.make_server(tmp_path, monkeypatch, ["done", "done"])
    sid = await m1.new_session(server, tmp_path)
    live = server.live[sid]
    live.run_result = "failed"  # an earlier turn's outcome must not survive the interrupted one either
    started = asyncio.Event()
    original = server.autonomy.prepare

    async def hang(session, text):
        started.set()
        await asyncio.Event().wait()  # inside the turn's try block: only task.cancel() gets it out

    server.autonomy.prepare = hang  # type: ignore[method-assign]
    await m1.rpc(server, "prompt.submit", {"text": "long task"})
    task = live.turn_task
    await asyncio.wait_for(started.wait(), 10)
    assert live.streaming and live.state == "working"
    assert (await m1.rpc(server, "session.interrupt", {"session_id": sid}))["result"]["interrupted"]
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert task.cancelled()  # the cancellation propagated, nothing swallowed it
    assert not live.streaming and not live.turn_in_flight
    assert live.run_result == "completed" and live.state == "completed"
    row = next(r for r in (await m1.rpc(server, "session.active_list", {}))["result"]["sessions"] if r["id"] == sid)
    assert row["state"] == "completed"

    # the session takes the next prompt, and that turn starts from a cleared outcome
    seen: list[str | None] = []

    async def probe(session, text):
        seen.append(session.run_result)
        return await original(session, text)

    server.autonomy.prepare = probe  # type: ignore[method-assign]
    await m1.submit_and_wait(server, "next")
    assert seen == [None]
    assert live.run_result == "completed" and live.state == "completed"


async def test_a_working_row_times_the_turn_not_the_session_age(tmp_path, monkeypatch):
    """The agent view read "working 1370m" five minutes after start: the row's time came from the stored session's
    created_at. A turn started now must report a start time of now, whatever the session's age."""
    server, _ = m1.make_server(tmp_path, monkeypatch, ["done"])
    sid = await m1.new_session(server, tmp_path)
    live = server.live[sid]
    live.stored.created_at = time.time() - DAY  # a reused session, opened long ago
    started = asyncio.Event()

    async def hang(session, text):
        started.set()
        await asyncio.Event().wait()

    server.autonomy.prepare = hang  # type: ignore[method-assign]
    await m1.rpc(server, "prompt.submit", {"text": "long task"})
    task = live.turn_task
    await asyncio.wait_for(started.wait(), 10)
    row = next(r for r in (await m1.rpc(server, "session.active_list", {}))["result"]["sessions"] if r["id"] == sid)
    assert row["state"] == "working"
    assert abs(row["turn_started_at"] - time.time()) < 2
    assert row["started_at"] == live.stored.created_at  # the creation date stays, for age and sorting
    await m1.rpc(server, "session.interrupt", {"session_id": sid})
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


async def test_a_resumed_idle_session_reports_no_turn_time(tmp_path, monkeypatch):
    """Between turns there is nothing to time: a resumed session must not show its age since creation as elapsed."""
    server, _ = m1.make_server(tmp_path, monkeypatch, ["done"])
    old = StoredSession(
        session_id=server.store.new_id(),
        cwd=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        created_at=time.time() - DAY,
    )
    server.store.insert(old)
    await m1.rpc(server, "session.resume", {"session_id": old.session_id})
    rows = (await m1.rpc(server, "session.active_list", {}))["result"]["sessions"]
    row = next(r for r in rows if r["id"] == old.session_id)
    assert row["state"] != "working"
    assert row["turn_started_at"] is None
    assert row["started_at"] == old.created_at
