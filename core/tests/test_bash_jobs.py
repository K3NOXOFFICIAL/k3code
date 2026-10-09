"""E5: bash documents its timeout and runs background jobs (bash_output / bash_kill); T6: every tool states limits."""

from __future__ import annotations

import asyncio
import os
import re

import pytest

from k3code.agent.loop import AgentLoop
from k3code.config import Settings
from k3code.permissions import decide
from k3code.providers.types import ToolCall
from k3code.research.tools import register_web_tools
from k3code.router import Router, build_chain
from k3code.tools import build_registry, jobs, tool_bash, tool_bash_kill, tool_bash_output
from test_permissions_gateway import call, make_server


def _job_id(result: dict) -> str:
    m = re.search(r"started (job\d+) \(pid \d+\)", result["content"])
    assert m, result
    return m.group(1)


async def _read_until_exit(job_id: str, session: str) -> str:
    seen = ""
    for _ in range(200):
        out = (await tool_bash_output({"job_id": job_id}, session_id=session))["content"]
        seen += out + "\n"
        if "exited with code" in out:
            return seen
        await asyncio.sleep(0.02)
    raise AssertionError(f"job never exited: {seen}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
async def clean_jobs():
    yield
    await jobs.REGISTRY.reap_all()


async def test_timeout_says_how_long_and_how_to_go_on(tmp_path):
    res = await tool_bash({"command": "sleep 5", "timeout": 0.3}, cwd=tmp_path)
    assert res["error"] == "Command timed out after 0.3s; retry with timeout up to 600 or background: true"


def test_bash_schema_documents_default_and_max_timeout():
    spec, _ = build_registry().get("bash")
    timeout = spec.parameters["properties"]["timeout"]
    assert timeout["default"] == 30 and "default 30, max 600" in timeout["description"]
    assert "Seconds" in timeout["description"] and spec.parameters["properties"]["background"]["type"] == "boolean"


async def test_background_job_lifecycle(tmp_path, clean_jobs):
    started = await tool_bash(
        {"command": "echo one; echo err >&2; sleep 0.2; echo two", "background": True}, cwd=tmp_path, session_id="s1"
    )
    job = _job_id(started)
    assert [j["job_id"] for j in jobs.list_jobs("s1")] == [job] and jobs.list_jobs("s2") == []
    out = await _read_until_exit(job, "s1")
    assert "one" in out and "err" in out and "two" in out and f"[{job} exited with code 0]" in out
    again = (await tool_bash_output({"job_id": job}, session_id="s1"))["content"]
    assert again == f"[{job} exited with code 0]\n(no new output)"
    assert (await tool_bash_kill({"job_id": job}, session_id="s1"))[
        "content"
    ] == f"{job} had already exited with code 0"


async def test_kill_and_read_only_reach_jobs_of_the_same_session(tmp_path, clean_jobs):
    job = _job_id(await tool_bash({"command": "sleep 30", "background": True}, cwd=tmp_path, session_id="s1"))
    pid = jobs.list_jobs("s1")[0]["pid"]
    for tool in (tool_bash_kill, tool_bash_output):
        res = await tool({"job_id": job}, session_id="s2")
        assert res["error"].startswith(f"no job '{job}' in this session")
    assert _alive(pid)
    killed = await tool_bash_kill({"job_id": job}, session_id="s1")
    assert killed["content"].startswith(f"killed {job}")
    assert not _alive(pid)


async def test_reap_kills_every_job_of_the_session_only(tmp_path, clean_jobs):
    mine = [_job_id(await tool_bash({"command": "sleep 30", "background": True}, cwd=tmp_path, session_id="s3"))]
    mine.append(_job_id(await tool_bash({"command": "sleep 30", "background": True}, cwd=tmp_path, session_id="s3")))
    other = _job_id(await tool_bash({"command": "sleep 30", "background": True}, cwd=tmp_path, session_id="s4"))
    pids = [j["pid"] for j in jobs.list_jobs("s3")]
    assert await jobs.reap("s3") == 2
    assert jobs.list_jobs("s3") == [] and not any(_alive(p) for p in pids)
    assert [j["job_id"] for j in jobs.list_jobs("s4")] == [other] and jobs.list_jobs("s4")[0]["running"]


async def test_closing_a_session_reaps_its_jobs(tmp_path, monkeypatch, clean_jobs):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    sid = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    await tool_bash({"command": "sleep 30", "background": True}, cwd=tmp_path, session_id=sid)
    pid = jobs.list_jobs(sid)[0]["pid"]
    await server.close()
    assert jobs.list_jobs(sid) == [] and not _alive(pid)


async def test_close_live_reaps_the_sessions_jobs(tmp_path, monkeypatch, clean_jobs):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    sid = (await call(server, "session.create", {"cwd": str(tmp_path)}))["session_id"]
    await tool_bash({"command": "sleep 30", "background": True}, cwd=tmp_path, session_id=sid)
    pid = jobs.list_jobs(sid)[0]["pid"]
    for c in server.clients:
        c.session_id = None  # nobody is attached: the session may close
    assert (await server.close_live(sid))["closed"]
    assert jobs.list_jobs(sid) == [] and not _alive(pid)


class _NoProvider:
    name = "fake"
    base_url = "https://fake.test"

    async def stream(self, *a, **kw):  # pragma: no cover - never called
        if False:
            yield None

    async def aclose(self) -> None:
        pass


async def test_background_bash_goes_through_the_bash_permission_decision(tmp_path, clean_jobs):
    loop = AgentLoop(
        Router(build_chain([_NoProvider()], [["m"]]), max_retries=0),
        system_prompt="t",
        permission_mode="default",
        headless=True,
        cwd=tmp_path,
        session="sx",
    )
    fg = await loop._execute_tool(ToolCall(id="a", name="bash", arguments={"command": "sleep 30"}))
    bg = await loop._execute_tool(ToolCall(id="b", name="bash", arguments={"command": "sleep 30", "background": True}))
    assert fg == bg and "approval required" in bg["error"]
    assert jobs.list_jobs("sx") == []
    for tool in ("bash_output", "bash_kill"):
        assert decide(mode="default", tool=tool, args={"job_id": "job1"}, cwd=tmp_path).action == "allow"
    # the loop files a started job under its own session
    loop.permission_mode = "yolo"
    loop._sandbox_argv = lambda: None  # this test is about the session, not bwrap
    started = await loop._execute_tool(
        ToolCall(id="c", name="bash", arguments={"command": "sleep 30", "background": True})
    )
    job = _job_id(started)
    assert [j["job_id"] for j in jobs.list_jobs("sx")] == [job]
    out = await loop._execute_tool(ToolCall(id="d", name="bash_kill", arguments={"job_id": job}))
    assert out["content"].startswith(f"killed {job}")


def test_every_builtin_tool_states_its_limits():
    reg = build_registry()
    register_web_tools(reg, Settings())
    for name in ("read", "write", "edit", "bash", "bash_output", "bash_kill", "grep", "glob", "todo", "web_fetch"):
        spec, _ = reg.get(name)
        limits = [ln for ln in spec.description.splitlines() if ln.startswith("Limits: ")]
        assert len(limits) == 1, name
    assert "30 s by default" in reg.get("bash")[0].description
    assert "14000 chars" in reg.get("web_fetch")[0].description
