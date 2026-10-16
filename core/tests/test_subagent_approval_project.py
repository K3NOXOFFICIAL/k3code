"""A background sub-agent's "always" answer is stored in the project the child works in, not the parent's current one.

The child asks through the parent session's shared approval callback, which read the parent's live ``perms.cwd`` when
the user answered. After ``session.workspace.move`` (or the next turn's ``perms.cwd = stored.cwd``) that is another
project: the rule the user approved while looking at the old project's command landed in the new project's trusted
``.k3code/config.yaml``, and an edit preview was built against the new project's file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from k3code.gateway import server as server_mod
from k3code.subagents.runner import Handle, SubagentManager
from k3code.subagents.types import AgentType
from test_autonomy_gateway import make, start


def _allow_patterns(project: Path) -> list[str]:
    cfg = project / ".k3code" / "config.yaml"
    if not cfg.is_file():
        return []
    bash = ((yaml.safe_load(cfg.read_text()) or {}).get("permissions") or {}).get("bash") or {}
    return [pattern for pattern, action in bash.items() if action == "allow"]


async def _child_in(server, tmp_path: Path, project: Path):
    """A background-style child spawned in ``project``, wired the way ``_drive`` does it."""
    session = server.session
    session._approval_cb = await server._approval_callback_for(session)  # what _drive shares with each child
    handle = Handle(id="c1", description="d", agent_type="general", tier="main", depth=1, parent_sid=session.session_id)
    return SubagentManager(server).build_loop(session, handle, AgentType(name="general", tools=["bash"]), project, None)


async def test_always_from_a_child_is_stored_in_the_project_it_started_in(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], mode="default")
    project_a, project_b = tmp_path / "a", tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    await start(server, project_a)
    server.session.perms.cwd = project_a
    loop = await _child_in(server, tmp_path, project_a)

    server.session.perms.cwd = project_b  # session.workspace.move while the child still runs in A

    async def answer_always(method, params, session_id=None):
        return {"choice": "always"}

    monkeypatch.setattr(server, "_ask_client", answer_always)
    args = {"command": "make deploy"}
    decision = loop.permissions.decide("bash", args)
    assert decision.action == "ask"
    await loop.approval_callback("bash", args, decision)

    assert any("make deploy" in p for p in _allow_patterns(project_a))
    assert _allow_patterns(project_b) == []


async def test_a_child_approval_preview_is_built_against_the_childs_directory(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], mode="default")
    project_a, project_b = tmp_path / "a", tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    await start(server, project_a)
    server.session.perms.cwd = project_a
    loop = await _child_in(server, tmp_path, project_a)
    server.session.perms.cwd = project_b
    seen: list[Path] = []

    async def spy_preview(tool_name, arguments, cwd):
        seen.append(Path(cwd))
        return ""

    async def answer_once(method, params, session_id=None):
        return {"choice": "once"}

    monkeypatch.setattr(server_mod, "_change_preview", spy_preview)
    monkeypatch.setattr(server, "_ask_client", answer_once)
    args = {"path": "notes.txt", "content": "x"}
    await loop.approval_callback("write", args, loop.permissions.decide("write", args))
    assert seen == [project_a]


async def test_a_session_turns_own_always_still_follows_the_session_directory(tmp_path, monkeypatch):
    """The parent's own prompts keep reading its live cwd: only a child is pinned to where it started."""
    server = make(tmp_path, monkeypatch, [], mode="default")
    project_a, project_b = tmp_path / "a", tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    await start(server, project_a)
    session = server.session
    approve = await server._approval_callback_for(session)
    session.perms.cwd = project_b

    async def answer_always(method, params, session_id=None):
        return {"choice": "always"}

    monkeypatch.setattr(server, "_ask_client", answer_always)
    args = {"command": "make deploy"}
    await approve("bash", args, session.perms.decide("bash", args))
    assert any("make deploy" in p for p in _allow_patterns(project_b))
    assert _allow_patterns(project_a) == []
