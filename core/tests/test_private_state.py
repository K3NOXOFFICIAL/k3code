"""File modes do not come from the service unit's UMask: files the tools write into a project follow the user's
umask, and k3code's private state is 0600/0700 whatever the umask."""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from k3code import memory, paths
from k3code.gateway.sessions import SessionStore
from k3code.learning import distiller
from k3code.learning.decisions import DecisionLog
from k3code.providers.types import Message
from k3code.reliability.hooks import Reliability
from k3code.reliability.journal import ToolJournal
from k3code.setup import state
from k3code.tools import tool_edit, tool_write
from k3code.usage import UsageDB


@contextlib.contextmanager
def umask(value: int) -> Iterator[None]:
    old = os.umask(value)
    try:
        yield
    finally:
        os.umask(old)


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.mark.parametrize(("mask", "file_mode", "dir_mode"), [(0o022, 0o644, 0o755), (0o077, 0o600, 0o700)])
async def test_tool_writes_follow_the_umask(tmp_path: Path, mask: int, file_mode: int, dir_mode: int) -> None:
    with umask(mask):
        await tool_write({"path": "pkg/new.py", "content": "x = 1\n"}, cwd=tmp_path)
    assert mode(tmp_path / "pkg" / "new.py") == file_mode
    assert mode(tmp_path / "pkg") == dir_mode
    assert [p.name for p in (tmp_path / "pkg").iterdir()] == ["new.py"]  # no temp file left behind


async def test_an_edit_keeps_the_files_own_mode(tmp_path: Path) -> None:
    f = tmp_path / "run.sh"
    f.write_text("echo a\n")
    f.chmod(0o750)
    with umask(0o077):
        await tool_edit({"path": "run.sh", "old_string": "echo a", "new_string": "echo b"}, cwd=tmp_path)
    assert mode(f) == 0o750 and f.read_text() == "echo b\n"


def test_private_state_is_0600_and_0700_whatever_the_umask(tmp_path: Path) -> None:
    home = paths.home()
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    with umask(0):  # the loosest umask: every mode below is set explicitly, not inherited
        # first: the agent loop saves the transcript at turn start, before a tool call opens the journal
        Reliability(session="s0", home=home).save_transcript([Message(role="user", content="a secret")])
        assert mode(home / "journal") == 0o700
        stores = [SessionStore(home / "sessions.db"), UsageDB(home / "usage.db"), DecisionLog(home)]
        journal = ToolJournal(home, "s1")
        journal.record_intent("c1", "read", {"path": "x"}, side_effect=False)
        journal.close()
        learned = memory.write_learned(project, ["uses uv"])
        state.save_state({"completed": ["provider"], "data": {}})
    files = [
        home / "sessions.db",
        home / "usage.db",
        home / "learning" / "decisions.db",
        home / "journal" / "s1.jsonl",
        home / "journal" / "s0.messages.json",
        learned,
        state.state_path(),
    ]
    for f in files:
        assert mode(f) == 0o600, f
    for d in (home / "learning", home / "journal", learned.parent):
        assert mode(d) == 0o700, d
    for s in stores:
        with contextlib.suppress(AttributeError):
            s.close()


@pytest.mark.parametrize(
    "write",
    [
        lambda p: memory.append_memory(p, "prefers uv"),
        lambda p: distiller.add_user_line(p, "prefers uv"),
        lambda p: distiller.write_auto_section(p, []),
    ],
    ids=["append_memory", "add_user_line", "write_auto_section"],
)
def test_user_memory_is_0600_whatever_the_umask(write) -> None:
    user_md = memory.user_memory_path()
    with umask(0):
        write(user_md)
    assert mode(user_md) == 0o600
    assert mode(user_md.parent) == 0o700


def test_existing_private_files_are_tightened(tmp_path: Path) -> None:
    db = paths.home() / "sessions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.touch()
    db.chmod(0o644)  # written by an older daemon or the CLI under umask 0022
    SessionStore(db)
    assert mode(db) == 0o600


def test_learning_preferences_and_mem0_ledger_are_0600_whatever_the_umask(tmp_path: Path) -> None:
    with umask(0):
        prefs = distiller.write_preferences_json(tmp_path, [distiller.Preference("prefers uv", 0.9, 3, "uv")])
        ledger = tmp_path / "learning" / distiller.MEM0_POSTED
        distiller._save_posted(ledger, ["k1"])
    assert mode(prefs) == 0o600
    assert mode(ledger) == 0o600
    assert mode(prefs.parent) == 0o700


def test_setup_user_md_is_0600_whatever_the_umask() -> None:
    from k3code.setup import steps

    with umask(0):
        path = steps.write_user_md({"name": "Alice"})
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700


# ── $K3CODE_HOME and ~/.config/k3code are 0700 under the usual umask (issue #163) ──


def test_home_state_dirs_are_0700_under_umask_022(tmp_path: Path) -> None:
    """``mkdir(parents=True)`` gave $K3CODE_HOME (and ~/.config/k3code with the provider keys) the umask's 0755: every
    user on the machine could list them and read any file that was not 0600 itself. Each writer that can be the first
    to create them now makes them 0700."""
    from k3code import confio, halt, trust
    from k3code.artifacts import ArtifactStore
    from k3code.automation.store import AutomationDB
    from k3code.blockers import BlockerStore
    from k3code.permissions.state import log_decision
    from k3code.router.classifier import FailoverReason
    from k3code.router.cooldown import CooldownStore
    from k3code.setup import onboard
    from k3code.setup.steps import write_config

    home = paths.home()
    sub = home / "nested"  # a missing ancestor is created 0700 too

    def fresh() -> None:
        import shutil

        shutil.rmtree(home, ignore_errors=True)
        assert not home.exists()

    project = tmp_path / "proj"
    (project / ".k3code").mkdir(parents=True)
    (project / ".k3code" / "config.yaml").write_text("display: {}\n")  # something to trust
    writers = {
        "sessions": lambda: SessionStore(home / "sessions.db"),
        "usage": lambda: UsageDB(home / "usage.db"),
        "blockers": lambda: BlockerStore(home / "blockers.db"),
        "artifacts": lambda: ArtifactStore(home / "artifacts.db"),
        "automation": lambda: AutomationDB(sub / "automation.db"),
        "cooldowns": lambda: CooldownStore(path=home / "cooldowns.json").arm(
            FailoverReason.rate_limit, provider="p", model="m", retry_after=60
        ),
        "halt": lambda: halt.set_halt(home, "test"),
        "trust": lambda: trust.record(project, trusted=True) or pytest.fail("nothing to trust"),
        "decisions": lambda: log_decision(session="s", tool="bash", pattern="ls", choice="allow", cwd=str(project)),
        "onboarding": lambda: onboard.record_answer("fast"),
        "setup-state": lambda: state.save_state({"completed": [], "data": {}}),
        "config": lambda: write_config({}),
        "confio": lambda: confio.write_yaml(paths.user_config_path(), {"display": {}}),
    }
    for name, write in writers.items():
        fresh()
        with umask(0o022):
            write()
        assert home.is_dir() and mode(home) == 0o700, name
        if (sub).exists():
            assert mode(sub) == 0o700, name

    fresh()
    env = state.env_file_path()
    with umask(0o022):
        state.set_env_var("K3_TEST_KEY", "x")
    assert mode(env.parent) == 0o700 and mode(env) == 0o600

    from k3code import doctor

    fresh()
    with umask(0o022):
        assert doctor.check_home(home).status == doctor.OK  # `k3code doctor` on a fresh machine creates the home
    assert mode(home) == 0o700


async def test_debug_bundle_dir_is_0700_under_umask_022() -> None:
    from k3code import debugdump
    from test_gateway import make_server

    home = paths.home()
    server = make_server()
    with umask(0o022):
        bundle = await debugdump.write_bundle(server, 5, home=home, probe=False)
    assert mode(bundle.parent) == 0o700  # events and config, redacted but not for other users


def test_a_projects_k3code_dir_keeps_the_umask(tmp_path: Path) -> None:
    """A project's .k3code/ is part of the repository: its config and reports follow the user's umask as before."""
    from k3code import confio
    from k3code.permissions.rules import Rule
    from k3code.permissions.state import persist_rules

    project = tmp_path / "proj"
    project.mkdir()
    with umask(0o022):
        confio.write_yaml(paths.project_config_path(project), {"display": {}})
    assert mode(project / ".k3code") == 0o755
    other = tmp_path / "other"
    other.mkdir()
    with umask(0o022):
        persist_rules(other / ".k3code" / "config.yaml", [Rule(tool="bash", pattern="ls", action="allow")])
    assert mode(other / ".k3code") == 0o755


def test_startup_tightens_an_existing_world_readable_home() -> None:
    from click.testing import CliRunner

    from k3code import cli as cli_mod

    home = paths.home()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o755)  # made by an older version under umask 0022
    (home / "kept").mkdir(mode=0o755)
    result = CliRunner().invoke(cli_mod.cli, ["prompt", "dump", "--hash"])
    assert result.exit_code == 0, result.output
    assert mode(home) == 0o700
    assert mode(home / "kept") == 0o755  # only the home itself: its contents keep their own modes
    assert paths.tighten_home() is False  # already private: nothing to do


def test_tighten_home_leaves_a_missing_or_foreign_home_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    home = paths.home()
    assert not home.exists() or home.rmdir() is None
    assert paths.tighten_home() is False and not home.exists()  # never created here
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(home).st_uid + 1)  # someone else's directory
    assert paths.tighten_home() is False
    assert mode(home) == 0o755


def test_doctor_reports_the_home_mode() -> None:
    from k3code import doctor

    home = paths.home()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o755)
    check = doctor.check_home_mode(home)
    assert check.status == doctor.WARN and "0755" in check.detail and check.fix == f"chmod 700 {home}"
    home.chmod(0o700)
    check = doctor.check_home_mode(home)
    assert check.status == doctor.OK and "0700" in check.detail
