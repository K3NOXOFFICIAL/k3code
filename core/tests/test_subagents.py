"""task / task_result tools, depth limit, worktree isolation, agent types (scripted fake provider)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from k3code.permissions import PermissionMode
from k3code.reliability import sandbox
from k3code.subagents import DepthLimit
from k3code.subagents.types import load_agent_types, parse_agent_md
from k3code.tools import tool_bash
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


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_child_of_a_foreground_default_mode_parent_runs_bash_sandboxed(tmp_path, monkeypatch):
    steps = [task_call("CHILD-S run a shell command"), final("ok"), {"type": "text", "match": "CHILD-S", "text": "s"}]
    server = make(tmp_path, monkeypatch, steps, mode="default", **NO_GATE)
    (tmp_path / "proj").mkdir()  # the k3code home lives under tmp_path: a project must not contain it
    await start(server, tmp_path / "proj")
    await run_turn(server, "PARENT: delegate it")
    assert not server.session.background and server.session.perms.mode == PermissionMode.DEFAULT
    assert server.session.loop._sandbox_argv() is None  # the parent's own foreground bash is unchanged
    (h,) = server.subagents.handles.values()
    assert h.loop.unattended  # a sub-agent never has a human watching it
    assert h.loop._sandbox_argv()[0] == sandbox.bwrap_path()


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


async def test_a_failed_worktree_child_releases_its_checkout_and_keeps_its_work_on_the_branch(tmp_path, monkeypatch):
    """_finish_worktree only ran on the success path: a child that died (provider error, budget, hook failure) left a
    full checkout in .k3code/worktrees and its branch behind, unmentioned."""
    from k3code.subagents.runner import SubagentManager

    repo = git_repo(tmp_path / "repo")
    steps = [task_call("CHILD-F write", isolation="worktree"), final("parent done"),
             {"type": "tool_call", "match": "CHILD-F", "when": "first", "name": "write",
              "arguments": {"path": "partial.txt", "content": "half done\n"}},
             {"type": "text", "match": "CHILD-F", "when": "after_tool", "text": "wrote"}]
    server = make(tmp_path, monkeypatch, steps, **NO_GATE)
    await call(server, "session.create", {"cwd": str(repo)})
    real_drive = SubagentManager._drive

    async def die_after_writing(self, parent, h, atype, prompt, cwd):
        await real_drive(self, parent, h, atype, prompt, cwd)
        raise RuntimeError("boom after the child already wrote a file")

    monkeypatch.setattr(SubagentManager, "_drive", die_after_writing)
    await run_turn(server, "PARENT")
    (h,) = server.subagents.handles.values()
    assert h.status == "failed" and h.worktree is not None
    assert not h.worktree.path.exists(), "the failed child's checkout directory leaked"
    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert h.branch in branches  # its work is on the branch
    assert "partial.txt" in subprocess.run(["git", "show", "--stat", h.branch], cwd=repo, capture_output=True,
                                           text=True).stdout


async def test_waiting_on_a_child_does_not_swallow_a_stop(tmp_path, monkeypatch):
    """SubagentManager.wait() suppressed every CancelledError: /stop cancelled the parent turn while it sat in wait()
    and was lost, so ultracode/ultraplan/fan-out chains carried on."""
    import asyncio

    from k3code.subagents.runner import Handle, SubagentManager

    mgr = SubagentManager(server=None)
    h = Handle(id="sa-x", description="d", agent_type="worker", tier="main", depth=1, parent_sid="p",
               parent_child_id=None, isolation="none", index=0, count=1)
    child_cancelled: list[bool] = []

    async def long_child() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            child_cancelled.append(True)
            raise

    h.task = asyncio.create_task(long_child())
    waiter = asyncio.create_task(mgr.wait(h))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0.05)
    assert child_cancelled == [True]  # the child went down with the stopped parent
    # ...whereas the child's own interruption is just its result
    h2 = Handle(id="sa-y", description="d", agent_type="worker", tier="main", depth=1, parent_sid="p",
                parent_child_id=None, isolation="none", index=0, count=1)
    h2.task = asyncio.create_task(long_child())
    waiter2 = asyncio.create_task(mgr.wait(h2))
    await asyncio.sleep(0.05)
    h2.task.cancel()
    assert await waiter2 is h2


def test_child_reliability_keeps_the_configured_budgets(tmp_path):
    from k3code.config import Settings
    from k3code.subagents.runner import child_reliability

    cfg = Settings(providers=[], reliability={"session_tokens": 1000, "day_usd": 5.0})
    rel = child_reliability(cfg, "child-1", tmp_path)
    assert rel.governor is not None
    scopes = {b.scope: b for b in rel.governor._budgets.values()}
    assert scopes["session"].tokens == 1000 and scopes["day"].usd == 5.0
    assert rel.flags.netwatch is False  # still no netwatch per child


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_escape_git_in_a_sub_agent_worktree_reads_but_cannot_write_the_shared_hooks(tmp_path):
    from k3code.subagents import worktree

    repo = git_repo(tmp_path / "repo").resolve()
    child = await worktree.create(repo, "child1")
    assert child is not None
    cmd = ('git status --porcelain >/dev/null && echo git-reads-ok; '
           'echo planted > "$(git rev-parse --git-common-dir)/hooks/post-commit"; echo wrote=$?')
    res = await tool_bash({"command": cmd}, cwd=child.path, sandbox=sandbox.build_argv(child.path))
    assert "git-reads-ok" in res["stdout"]  # git works in the linked worktree
    assert "wrote=0" not in res["stdout"]  # but the hooks dir every worktree shares is read-only
    assert not (repo / ".git" / "hooks" / "post-commit").exists()

