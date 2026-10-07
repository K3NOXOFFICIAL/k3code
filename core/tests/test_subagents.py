"""task / task_result tools, depth limit, worktree isolation, agent types (scripted fake provider)."""

from __future__ import annotations

import subprocess

import pytest

from k3code.subagents import DepthLimit
from k3code.subagents.types import load_agent_types, parse_agent_md
from m1cmd_helpers import git_repo
from test_autonomy_gateway import call, events, make, models_called, run_turn, start

NO_GATE = {"autonomy": {"plan_first": False, "proposals": False}}


def task_call(prompt: str, *, match: str = "PARENT", when: str = "first", **args) -> dict:
    return {"type": "tool_call", "match": match, "when": when, "name": "task",
            "arguments": {"description": "child job", "prompt": prompt, **args}}


def final(text: str, match: str = "PARENT") -> dict:
    return {"type": "text", "match": match, "when": "after_tool", "text": text}


def last_tool_result(server) -> str:
    return next(m["content"] for m in reversed(server.session.stored.messages) if m["role"] == "tool")


async def test_task_sync_returns_child_answer_and_emits_events(tmp_path, monkeypatch):
    steps = [task_call("CHILD-A find the answer"), final("parent done"),
             {"type": "text", "match": "[agent:worker]", "text": "the answer is 42"}]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "PARENT: delegate it")
    assert last_tool_result(server) == "the answer is 42"
    assert server.session.stored.messages[-1]["content"] == "parent done"
    kinds = [e for e in ("subagent.spawn_requested", "subagent.start", "subagent.complete") if events(server, e)]
    assert kinds == ["subagent.spawn_requested", "subagent.start", "subagent.complete"]
    done = events(server, "subagent.complete")[0]
    assert done["status"] == "completed" and done["summary"] == "the answer is 42" and done["depth"] == 0
    assert done["goal"] == "child job" and done["parent_id"] is None
    (h,) = server.subagents.for_session(server.session.session_id)
    assert h.tier == "main" and h.agent_type == "worker"


async def test_task_child_uses_agent_type_tier_and_readonly_tools(tmp_path, monkeypatch):
    steps = [task_call("CHILD-E look around", agent_type="explorer"), final("ok"),
             {"type": "text", "match": "[agent:explorer]", "text": "explored"}]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "PARENT: explore")
    child = next(c for p in server.providers for c in p.log if "[agent:explorer]" in c["text"])
    assert child["model"] == "m-cheap"  # explorer's tier
    assert set(child["tools"]) == {"read", "grep", "glob"}


async def test_task_unknown_agent_type_is_a_tool_error(tmp_path, monkeypatch):
    steps = [task_call("x", agent_type="nope"), final("ok")]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "PARENT")
    assert "unknown agent_type" in last_tool_result(server)


async def test_task_background_handle_and_task_result(tmp_path, monkeypatch):
    steps = [
        task_call("CHILD-B work", background=True),
        {"type": "tool_call", "match": "PARENT", "when": "after_tool", "id": "c2", "name": "task_result",
         "arguments": {"id": "PLACEHOLDER", "wait": True}},
        {"type": "text", "match": "[agent:worker]", "text": "bg result"},
    ]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    await call(server, "prompt.submit", {"text": "PARENT: go"})
    sess = server.session
    # the second scripted call needs the real id: run the first turn, then poll manually
    await sess.turn_task
    (h,) = server.subagents.for_session(sess.session_id)
    assert "Started sub-agent" in next(m["content"] for m in sess.stored.messages if m["role"] == "tool")
    await server.subagents.wait(h)
    assert h.status == "completed" and h.result == "bg result"


async def test_depth_limit_two(tmp_path, monkeypatch):
    steps = [
        task_call("CHILD-L1 spawn one more"),
        final("p done"),
        {"type": "tool_call", "match": "CHILD-L1", "when": "first", "name": "task",
         "arguments": {"description": "grandchild", "prompt": "CHILD-L2 leaf"}},
        {"type": "text", "match": "CHILD-L1", "when": "after_tool", "text": "l1 done"},
        {"type": "text", "match": "CHILD-L2", "text": "leaf done"},
    ]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "PARENT")
    calls = [c for p in server.providers for c in p.log]
    l1 = next(c for c in calls if "CHILD-L1" in c["text"] and "CHILD-L2" not in c["text"])
    l2 = next(c for c in calls if "CHILD-L2" in c["text"])
    assert "task" in l1["tools"] and "task" not in l2["tools"]  # depth 2 cannot delegate further
    depths = {h.description: h.depth for h in server.subagents.handles.values()}
    assert sorted(depths.values()) == [1, 2]
    grand = next(h for h in server.subagents.handles.values() if h.depth == 2)
    assert grand.parent_child_id is not None
    sess = server.session
    with pytest.raises(DepthLimit):
        server.subagents.spawn(sess, description="x", prompt="x", depth=3)


async def test_worktree_isolation_merges_diff_back(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    steps = [
        task_call("CHILD-W write a file", isolation="worktree"), final("parent done"),
        {"type": "tool_call", "match": "CHILD-W", "when": "first", "name": "write",
         "arguments": {"path": "new.txt", "content": "hello\n"}},
        {"type": "text", "match": "CHILD-W", "when": "after_tool", "text": "wrote it"},
    ]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await call(server, "session.create", {"cwd": str(repo)})
    await run_turn(server, "PARENT")
    out = last_tool_result(server)
    assert "merged into your checkout" in out and "new.txt" in out
    assert (repo / "new.txt").read_text() == "hello\n"
    (h,) = server.subagents.handles.values()
    assert "+hello" in h.diff and h.merge == "merged"
    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert "k3/" not in branches  # merged branch is gone
    assert not (repo / ".k3code" / "worktrees" / h.id).exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert ".k3code" not in status  # worktrees never dirty the parent repo


async def test_worktree_conflict_leaves_branch(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    (repo / "clash.txt").write_text("parent version\n")  # untracked in the parent: the child's merge cannot land
    steps = [
        task_call("CHILD-C write", isolation="worktree"), final("parent done"),
        {"type": "tool_call", "match": "CHILD-C", "when": "first", "name": "write",
         "arguments": {"path": "clash.txt", "content": "child version\n"}},
        {"type": "text", "match": "CHILD-C", "when": "after_tool", "text": "wrote"},
    ]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await call(server, "session.create", {"cwd": str(repo)})
    await run_turn(server, "PARENT")
    (h,) = server.subagents.handles.values()
    assert h.merge == "conflict" and h.branch.startswith("k3/")
    assert "MERGE CONFLICT" in last_tool_result(server) and h.branch in last_tool_result(server)
    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert h.branch in branches
    assert (repo / "clash.txt").read_text() == "parent version\n"  # parent tree untouched


async def test_worktree_outside_git_repo_shares_cwd(tmp_path, monkeypatch):
    steps = [task_call("CHILD-N hi", isolation="worktree"), final("ok"),
             {"type": "text", "match": "CHILD-N", "text": "no repo"}]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "PARENT")
    assert last_tool_result(server) == "no repo"


async def test_children_get_own_reliability_and_do_not_share_parents(tmp_path, monkeypatch):
    steps = [task_call("CHILD-R go"), final("ok"), {"type": "text", "match": "CHILD-R", "text": "r"}]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await start(server, tmp_path)
    await run_turn(server, "PARENT")
    (h,) = server.subagents.handles.values()
    assert h.loop.reliability is not server.session.reliability
    assert h.loop.reliability.netwatch is None  # no probe per child
    assert models_called(server)  # sanity


def test_agent_type_files_and_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    scribe = "---\nname: scribe\ndescription: writes docs\ntools: read, write\ntier: cheap\n---\nWrite docs.\n"
    (home / "agents" / "scribe.md").write_text(scribe)
    proj = tmp_path / "proj"
    (proj / ".k3code" / "agents").mkdir(parents=True)
    override = "---\nname: explorer\ntools: read\n---\nProject explorer.\n"
    (proj / ".k3code" / "agents" / "explorer.md").write_text(override)
    types = load_agent_types(proj, home)
    assert {"explorer", "worker", "reviewer", "planner", "scribe"} <= set(types)
    assert types["scribe"].tools == ["read", "write"] and types["scribe"].tier == "cheap"
    assert types["explorer"].tools == ["read"] and types["explorer"].source == "project"  # project overrides built-in
    assert types["reviewer"].tier == "strong" and "VERDICT" in types["reviewer"].prompt
    assert parse_agent_md("no frontmatter", "x").name == "x"
