"""`process.list` and `paste.collapse`: the TUI called both and the gateway answered "Method not found" (the errors
were swallowed, so the Processes dock stayed empty and a collapsed paste never got its stored path)."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from k3code.tools import jobs, tool_bash
from test_autonomy_gateway import call, k3home, make, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}
#: Every field tui/src/app/processRoster.ts reads from a ProcessEntry, plus what the dock needs to show a process.
ENTRY_KEYS = {
    "session_id",
    "command",
    "status",
    "uptime_seconds",
    "exit_code",
    "exited_at",
    "completion_reason",
    "output_preview",
    "pid",
    "started",
    "cwd",
}


@pytest.fixture
async def server(tmp_path, monkeypatch):
    srv = make(tmp_path, monkeypatch, [{"type": "text", "text": "ok"}], **NO_GATE)
    await start(srv, tmp_path)
    yield srv
    await jobs.REGISTRY.reap_all()


async def _until(pred, what: str):
    for _ in range(500):
        value = await pred()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def test_process_list_shows_a_background_job_with_its_output_and_exit(server, tmp_path):
    sid = server.session.session_id
    started = await tool_bash(
        {"command": "echo first; echo second; sleep 30", "background": True},
        cwd=tmp_path,
        session_id=sid,
    )
    job_id = started["content"].split()[1]

    async def preview():
        procs = (await call(server, "process.list", {"session_id": sid}))["processes"]
        return procs if procs and "second" in procs[0]["output_preview"] else None

    procs = await _until(preview, "the job's output")
    assert len(procs) == 1 and set(procs[0]) >= ENTRY_KEYS
    entry = procs[0]
    assert entry["session_id"] == job_id and entry["kind"] == "background"
    assert entry["status"] == "running" and entry["exit_code"] is None and entry["exited_at"] is None
    assert entry["cwd"] == str(tmp_path) and entry["pid"] > 0 and "echo first" in entry["command"]
    assert entry["uptime_seconds"] >= 0

    await jobs.REGISTRY.kill(sid, job_id)
    entry = (await call(server, "process.list", {"session_id": sid}))["processes"][0]
    assert entry["status"] == "exited" and entry["exited_at"] is not None and entry["exit_code"] is not None
    # another session sees none of it; neither does an unknown one
    assert (await call(server, "process.list", {"session_id": "nope"}))["processes"] == []


async def test_process_list_shows_a_running_foreground_command_until_it_returns(server, tmp_path):
    sid = server.session.session_id
    run = asyncio.ensure_future(tool_bash({"command": "sleep 0.5; echo done"}, cwd=tmp_path, session_id=sid))

    async def foreground():
        procs = (await call(server, "process.list", {"session_id": sid}))["processes"]
        return [p for p in procs if p["kind"] == "foreground"]

    try:
        fg = await _until(foreground, "the foreground command")
        assert fg[0]["status"] == "running" and fg[0]["command"] == "sleep 0.5; echo done"
        assert fg[0]["cwd"] == str(tmp_path) and fg[0]["session_id"] == f"fg{fg[0]['pid']}"
    finally:
        out = await run  # the command runs to its end either way: no task outlives the test's event loop
    assert out["stdout"].strip() == "done"
    assert (await call(server, "process.list", {"session_id": sid}))["processes"] == []


async def test_paste_collapse_stores_the_text_in_the_sessions_folder(server, tmp_path):
    text = "line one\n" * 400
    res = await call(server, "paste.collapse", {"text": text})  # the TUI sends no session id
    path = Path(res["path"])
    assert path.parent == k3home(tmp_path) / "pastes" / server.session.session_id
    assert path.read_text() == text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (await call(server, "paste.collapse", {"text": text}))["path"] == res["path"]  # same text, same file


async def test_paste_collapse_never_turns_a_given_session_id_into_a_path(server, tmp_path):
    res = await call(server, "paste.collapse", {"text": "x", "session_id": "../../escape"})
    assert Path(res["path"]).parent == k3home(tmp_path) / "pastes" / server.session.session_id
