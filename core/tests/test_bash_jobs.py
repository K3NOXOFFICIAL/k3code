"""E5: background bash jobs (bash_output / bash_kill), owned by one session and reaped when it ends.

The timeout text, the bash schema and the tool limits are in test_bash_timeout_docs.py."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal

import pytest

from k3code.agent.loop import AgentLoop
from k3code.config import Settings
from k3code.permissions import decide
from k3code.providers.types import ToolCall
from k3code.router import Router, build_chain
from k3code.tools import jobs, tool_bash, tool_bash_kill, tool_bash_output
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


def _record_jobs(monkeypatch) -> list:
    """Every job the registry starts, kept even after a reap forgets it."""
    started: list = []
    real_add = jobs.REGISTRY.add

    def add(*a, **kw):
        job = real_add(*a, **kw)
        started.append(job)
        return job

    monkeypatch.setattr(jobs.REGISTRY, "add", add)
    return started


async def test_a_finished_sub_agent_reaps_its_background_jobs(tmp_path, monkeypatch, clean_jobs):
    from k3code.subagents import runner
    from test_autonomy_gateway import make, run_turn, start

    real_build = runner.SubagentManager.build_loop

    def unsandboxed(self, *args, **kwargs):  # this test is about the job's owner, not bwrap
        loop = real_build(self, *args, **kwargs)
        loop._sandbox_argv = lambda: None
        return loop

    monkeypatch.setattr(runner.SubagentManager, "build_loop", unsandboxed)
    started = _record_jobs(monkeypatch)
    steps = [
        {
            "type": "tool_call",
            "match": "CHILD-J",
            "when": "first",
            "name": "bash",
            "arguments": {"command": "sleep 30", "background": True},
        },
        {"type": "text", "match": "CHILD-J", "when": "after_tool", "text": "started it"},
        {
            "type": "tool_call",
            "match": "PARENT",
            "when": "first",
            "name": "task",
            "arguments": {"description": "child job", "prompt": "CHILD-J start a server"},
        },
        {"type": "text", "match": "PARENT", "when": "after_tool", "text": "parent done"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="yolo", autonomy={"plan_first": False, "proposals": False})
    (tmp_path / "proj").mkdir()
    await start(server, tmp_path / "proj")
    await run_turn(server, "PARENT: delegate it")
    (h,) = server.subagents.for_session(server.session.session_id)
    (job,) = started
    assert job.session_id == h.id and h.status == "completed"
    # the parent session is still open: only the child's finish can have ended the job
    assert jobs.list_jobs(h.id) == [] and not _alive(job.proc.pid)


@pytest.mark.parametrize("mode", ["headless", "repl"])
def test_a_cli_run_reaps_its_background_jobs_at_exit(tmp_path, monkeypatch, mode):
    from k3code import cli as cli_mod
    from k3code.config import ProviderEntry
    from k3code.permissions import PermissionMode
    from test_permissions_gateway import ScriptedProvider

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    (tmp_path / "proj").mkdir()
    monkeypatch.chdir(tmp_path / "proj")
    import k3code.providers as providers_mod

    bg = ToolCall(id="c1", name="bash", arguments={"command": "sleep 30", "background": True})
    monkeypatch.setattr(providers_mod, "make_providers", lambda entries: [ScriptedProvider([bg, "started it"])])
    monkeypatch.setattr(AgentLoop, "_sandbox_argv", lambda self: None)  # about the run's exit, not bwrap
    started = _record_jobs(monkeypatch)
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    config = Settings(providers=[prov], default_model="default")
    try:
        if mode == "headless":
            result = asyncio.run(
                cli_mod._run_headless(
                    "go", model=None, permission_mode=PermissionMode.YOLO, config=config, json_output=True
                )
            )
            assert result is not None and result.get("text") == "started it", result
        else:
            inputs = iter(["go", "/exit"])
            monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
            asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
        (job,) = started
        assert jobs.list_jobs(job.session_id) == [] and not _alive(job.proc.pid)
    finally:  # the run's event loop is gone: end a job it left behind directly
        for job in started:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(job.proc.pid, signal.SIGKILL)
            jobs.REGISTRY._jobs.pop(job.id, None)
