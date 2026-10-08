"""/fork and /branch (temp git repo)."""

from __future__ import annotations

import subprocess

from m1cmd_helpers import cmd, git_repo, make_server, new_session


def git(path, *a):
    return subprocess.run(["git", *a], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


async def test_fork_copies_session(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    live = server.session
    live.stored.messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    live.stored.model = "cheap"
    live.stored.meta["mode"] = "plan"
    server.store.save(live.stored)

    res = await cmd(server, "/fork my copy", sid)
    new = server.store.get(res["session_id"])
    assert new.session_id != sid and new.title == "my copy"
    assert new.messages == live.stored.messages and new.messages is not live.stored.messages
    assert new.cwd == str(tmp_path) and new.model == "cheap" and new.meta["mode"] == "plan"
    assert new.meta["forked_from"] == sid
    assert server.session.session_id == sid  # not activated

    res = await cmd(server, "/fork --activate", sid)
    assert server.session.session_id == res["session_id"] and res["activated"]
    assert server.store.get(res["session_id"]).title.endswith("(fork)") or server.store.get(res["session_id"]).title
    await server.close()


async def test_fork_requires_session(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    assert "No active session" in (await cmd(server, "/fork"))["output"]
    await server.close()


async def test_branch_creates_git_branch_and_links(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, repo)
    server.session.stored.messages = [{"role": "user", "content": "x"}]
    server.store.save(server.session.stored)

    res = await cmd(server, "/branch feature/x", sid)
    assert res["branch"] == "feature/x", res
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feature/x"
    new = server.store.get(res["session_id"])
    assert new.meta["branch"] == "feature/x" and new.meta["branched_from"] == sid and new.cwd == str(repo)
    parent = server.store.get(sid)
    assert parent.meta["branches"] == [{"name": "feature/x", "session_id": new.session_id}]
    assert new.messages == [{"role": "user", "content": "x"}]

    again = await cmd(server, "/branch feature/x", sid)  # branch exists → git error surfaced
    assert "failed" in again["output"]
    bad = await cmd(server, "/branch -evil", sid)
    assert "Invalid branch name" in bad["output"]
    await server.close()


async def test_branch_worktree(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, repo)
    res = await cmd(server, "/branch wt1 --worktree --activate", sid)
    wt = repo / ".k3code" / "worktrees" / "wt1"
    assert wt.is_dir() and (wt / "a.py").is_file(), res
    assert res["cwd"] == str(wt)
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # original checkout untouched
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "wt1"
    assert server.store.get(res["session_id"]).cwd == str(wt)
    assert server.session.session_id == res["session_id"]
    await server.close()


async def test_branch_refuses_outside_git(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, plain)
    res = await cmd(server, "/branch foo", sid)
    assert "Not a git repository" in res["output"]
    assert len(server.store.list()) == 1  # nothing forked
    await server.close()
