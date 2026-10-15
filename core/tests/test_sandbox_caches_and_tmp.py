"""The sandbox shows the cargo and go package caches read-only, and gives each session a private ``/tmp`` that
keeps its files between bash calls.

``cargo build`` and ``go build`` with their dependencies already fetched failed inside the sandbox: ``$HOME`` is an
empty tmpfs, so ``~/.cargo/registry`` and ``~/go/pkg/mod`` were not there and the build tried to download them (or
failed offline). And ``/tmp`` was a fresh tmpfs per call, so ``pytest > /tmp/out.txt`` followed by ``tail /tmp/out.txt``
in the next call found nothing.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from k3code import paths
from k3code.reliability import sandbox
from k3code.tools import tool_bash

BWRAP = "/usr/bin/bwrap"


def _proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return proj


def _ro_binds(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]


def _binds(argv: list[str]) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--bind"]


def _toolchain_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    for rel in ("registry/index", "git/db"):
        (home / ".cargo" / rel).mkdir(parents=True)
    (home / ".cargo" / "credentials.toml").write_text('[registry]\ntoken = "fake-cargo-token"\n')
    (home / ".cargo" / "credentials").write_text('token = "fake-cargo-token-old"\n')
    (home / "go" / "pkg" / "mod" / "cache").mkdir(parents=True)
    (home / "go" / "pkg" / "mod" / "cache" / "x.txt").write_text("cached module")
    return home


# ── ~/.cargo/registry, ~/.cargo/git and ~/go/pkg/mod ──


def test_package_caches_are_bound_read_only_after_the_home_tmpfs(tmp_path):
    home = _toolchain_home(tmp_path)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    ro = _ro_binds(argv)
    for rel in sandbox.HOME_PACKAGE_CACHES:
        assert str(home / rel) in ro, rel
        assert argv.index("--tmpfs") < argv.index(str(home / rel))  # below the tmpfs, not hidden by it
    # read-only: a build with cached deps needs no write there, and a writable bind would let a sandboxed command
    # change a crate source every host build compiles
    assert not any(src.startswith(str(home / ".cargo")) or src.startswith(str(home / "go")) for src, _ in _binds(argv))


def test_cargo_credentials_stay_hidden(tmp_path):
    home = _toolchain_home(tmp_path)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    joined = " ".join(argv)
    assert str(home / ".cargo") + " " not in joined and str(home / ".cargo/credentials") not in joined
    assert ".cargo/credentials" in sandbox.HOME_RO_HIDDEN and ".cargo/credentials.toml" in sandbox.HOME_RO_HIDDEN


def test_missing_package_caches_are_not_bound(tmp_path):
    home = tmp_path / "home"
    (home / ".cargo" / "registry").mkdir(parents=True)
    ro = _ro_binds(sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[]))
    assert str(home / ".cargo/registry") in ro
    assert str(home / ".cargo/git") not in ro and str(home / "go/pkg/mod") not in ro


def test_a_package_cache_that_links_to_a_credential_store_is_skipped(tmp_path):
    """``~/.cargo/registry -> ~/.cargo`` (or any link that reaches the credentials) must not bring them back."""
    home = tmp_path / "home"
    (home / ".cargo").mkdir(parents=True)
    (home / ".cargo" / "credentials.toml").write_text("token")
    os.symlink(home / ".cargo", home / ".cargo" / "registry")
    (home / "go" / "pkg").mkdir(parents=True)
    os.symlink(home, home / "go" / "pkg" / "mod")  # back to $HOME itself
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    ro = _ro_binds(argv)
    assert str(home / ".cargo/registry") not in ro and str(home / "go/pkg/mod") not in ro


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_package_caches_are_readable_and_credentials_are_not_inside_the_real_sandbox(tmp_path):
    home = _toolchain_home(tmp_path)
    proj = _proj(tmp_path)
    argv = sandbox.build_argv(proj, home=home, home_readonly=[])
    cmd = (
        f"cat {home}/go/pkg/mod/cache/x.txt; ls {home}/.cargo/registry; "
        f"cat {home}/.cargo/credentials.toml {home}/.cargo/credentials; "
        f"touch {home}/.cargo/registry/planted && echo PLANTED"
    )
    res = await tool_bash({"command": cmd}, cwd=proj, sandbox=argv)
    assert "cached module" in res["stdout"] and "index" in res["stdout"]
    assert "fake-cargo-token" not in res["stdout"] and "PLANTED" not in res["stdout"]
    assert res["stderr"].count("No such file") == 2 and "Read-only file system" in res["stderr"]
    assert not (home / ".cargo" / "registry" / "planted").exists()


# ── a private /tmp per session ──


def test_session_tmp_dir_is_private_under_the_k3code_home(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    d = sandbox.session_tmp_dir("abc123")
    assert d == paths.home() / sandbox.SESSION_TMP / "abc123" and d.is_dir()
    assert d.stat().st_mode & 0o777 == 0o700 and d.parent.stat().st_mode & 0o777 == 0o700
    assert sandbox.session_tmp_dir(d.name) == d  # the same directory for the next call of that session
    for bad in ("", ".", "..", "a/b", "a\\b", "x\0y"):
        assert sandbox.session_tmp_dir(bad) is None, bad
    assert not (paths.home() / sandbox.SESSION_TMP / "..").exists() or (paths.home() / sandbox.SESSION_TMP).is_dir()


def test_build_argv_binds_the_session_tmp_at_slash_tmp_or_a_tmpfs_without_one(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    own = tmp_path / "k3home" / "tmp" / "sid1"
    own.mkdir(parents=True)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[], tmp_dir=own)
    assert (str(own), "/tmp") in _binds(argv)
    assert "/tmp" not in [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert argv.index("/tmp") < argv.index("--tmpfs")  # before $HOME is masked, like the tmpfs was
    plain = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    assert "/tmp" in [plain[i + 1] for i, a in enumerate(plain) if a == "--tmpfs"]
    assert not any(dst == "/tmp" for _, dst in _binds(plain))


def test_the_read_only_home_view_takes_the_session_tmp_too(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    own = tmp_path / "k3home" / "tmp" / "sid1"
    own.mkdir(parents=True)
    argv = sandbox.build_argv(home, home=home, bwrap=BWRAP, readonly_home=True, tmp_dir=own)
    assert (str(own), "/tmp") in _binds(argv)


def test_agent_loop_passes_its_session_tmp_to_every_sandbox_argv(tmp_path, monkeypatch):
    from k3code.agent.loop import AgentLoop

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    monkeypatch.setattr(sandbox, "usable", lambda: True)
    seen: list[Path | None] = []

    def fake_build(cwd, add_dirs=(), **kw):
        seen.append(kw.get("tmp_dir"))
        return ["bwrap"]

    monkeypatch.setattr(sandbox, "build_argv", fake_build)

    class _R:
        chain: list = []

    proj = _proj(tmp_path)
    loop = AgentLoop(_R(), system_prompt="x", cwd=proj, permission_mode="yolo", session="sess42")  # type: ignore[arg-type]
    loop._sandbox_argv()
    loop._contained_argv()
    expected = tmp_path / "k3home" / "tmp" / "sess42"
    assert seen == [expected, expected] and expected.is_dir()


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_tmp_files_survive_between_sandboxed_calls_of_a_session_but_not_across_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    home = tmp_path / "home"
    home.mkdir()
    proj = _proj(tmp_path)
    (Path("/tmp") / "k3code-host-marker-should-not-show").touch()
    try:
        first = sandbox.build_argv(proj, home=home, home_readonly=[], tmp_dir=sandbox.session_tmp_dir("s1"))
        res = await tool_bash({"command": "echo kept > /tmp/out.txt; ls /tmp"}, cwd=proj, sandbox=first)
        # the host's /tmp is not what the sandbox sees (pytest's own basetemp may sit under the host's /tmp)
        assert "out.txt" in res["stdout"].split() and "k3code-host-marker-should-not-show" not in res["stdout"]
        again = sandbox.build_argv(proj, home=home, home_readonly=[], tmp_dir=sandbox.session_tmp_dir("s1"))
        res = await tool_bash({"command": "cat /tmp/out.txt"}, cwd=proj, sandbox=again)
        assert res["stdout"].strip() == "kept"
        other = sandbox.build_argv(proj, home=home, home_readonly=[], tmp_dir=sandbox.session_tmp_dir("s2"))
        res = await tool_bash({"command": "ls /tmp"}, cwd=proj, sandbox=other)
        # (the project itself sits under the host's /tmp here, so its bind leaves an empty mount point in the view)
        assert "out.txt" not in res["stdout"].split() and "k3code-host-marker-should-not-show" not in res["stdout"]
        assert (tmp_path / "k3home" / "tmp" / "s1" / "out.txt").read_text() == "kept\n"
    finally:
        (Path("/tmp") / "k3code-host-marker-should-not-show").unlink()
    assert not Path("/tmp/out.txt").exists() or Path("/tmp/out.txt").read_text() != "kept\n"


def test_remove_and_prune_session_tmps(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    home = paths.home()
    for sid in ("live1", "gone1", "gone2"):
        (sandbox.session_tmp_dir(sid) / "f").write_text("x")
    sandbox.remove_session_tmp(home, "gone1")
    assert not (home / "tmp" / "gone1").exists() and (home / "tmp" / "gone2").is_dir()
    sandbox.remove_session_tmp(home, "..")  # never the k3code home itself
    sandbox.remove_session_tmp(home, "tmp/../gone2")
    assert home.is_dir() and (home / "tmp" / "gone2").is_dir()
    # a link in a session's place is not followed: nothing on the host is removed through it
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    os.symlink(outside, home / "tmp" / "linked")
    sandbox.remove_session_tmp(home, "linked")
    assert sandbox.prune_session_tmps(home, keep=lambda sid: sid == "live1") == 1
    assert (home / "tmp" / "live1" / "f").is_file() and not (home / "tmp" / "gone2").exists()
    assert (outside / "keep").is_file()
    assert sandbox.prune_session_tmps(tmp_path / "nowhere", keep=lambda sid: False) == 0


async def test_server_removes_the_session_tmp_when_the_session_ends(tmp_path, monkeypatch):
    from m1cmd_helpers import make_server, new_session

    server, _ = make_server(tmp_path, monkeypatch)
    try:
        sid = await new_session(server, tmp_path / "proj")
        own = sandbox.session_tmp_dir(sid)
        assert own is not None and own.is_dir() and own.parent.parent == server._home()
        for client in server.clients:  # the test client is on the session; a session in use is not closed
            client.session_id = None
        assert (await server.close_live(sid))["closed"]
        assert not own.exists()

        sid2 = await new_session(server, tmp_path / "proj")
        own2 = sandbox.session_tmp_dir(sid2)
        stale = sandbox.session_tmp_dir("stale-from-last-daemon")
        assert own2 is not None and own2.is_dir() and stale is not None and stale.is_dir()
        server.compact_storage()  # daemon start: tmps of sessions that are not live go
        assert own2.is_dir() and not stale.exists()
        server.remove_session_files(sid2)  # session.delete / the empty-session sweep
        assert not own2.exists()
    finally:
        await server.close()
