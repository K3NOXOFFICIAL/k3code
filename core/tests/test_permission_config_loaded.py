"""Every loop applies the configured ``permissions:`` rules and ``permissions.hardline`` patterns, however it was built.

Only the gateway reloaded its session's PermissionState. `k3code -p` and the line REPL let AgentLoop build its own,
and a sub-agent or the plan-first planner got a fresh one: none of them ever read the config, so a deny rule or an
extra hardline pattern refused nothing there, in any mode (K3NOX-564)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from k3code import cli as cli_mod
from k3code import trust
from k3code.config import ProviderEntry, Settings
from k3code.permissions import PermissionMode
from k3code.permissions.state import PermissionState
from k3code.providers.fake import FakeProvider

DENY_RULE = 'permissions:\n  bash:\n    "touch *": deny\n'
HARDLINE = "permissions:\n  hardline: ['\\btouch\\b']\n"
CONFIGS = pytest.mark.parametrize("config_yaml", [DENY_RULE, HARDLINE], ids=["deny-rule", "hardline"])
# yolo ignores deny rules for shell commands (README, Permission modes); the hardline refuses in every mode
REFUSING = pytest.mark.parametrize(
    ("mode", "config_yaml"),
    [(PermissionMode.AUTO, DENY_RULE), (PermissionMode.AUTO, HARDLINE), (PermissionMode.YOLO, HARDLINE)],
    ids=["auto-deny-rule", "auto-hardline", "yolo-hardline"],
)
# the model asks to run `touch ran` once, then answers
STEPS = [
    {"type": "tool_call", "name": "bash", "arguments": {"command": "touch ran"}, "when": "turn_first"},
    {"type": "text", "text": "finished", "when": "turn_after_tool"},
]


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_yaml: str = "") -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    monkeypatch.setenv("K3CODE_HOME", str(home))
    return home


def _project(tmp_path: Path, config_yaml: str, *, trusted: bool) -> Path:
    root = tmp_path / "proj"
    (root / ".k3code").mkdir(parents=True)
    (root / ".k3code" / "config.yaml").write_text(config_yaml, encoding="utf-8")
    if trusted:
        assert trust.record(root, trusted=True)
    return root


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_yaml: str = "") -> tuple[Settings, Path]:
    """A CLI run in ``tmp_path/work`` whose model calls ``touch ran``; returns (config, work dir)."""
    _home(monkeypatch, tmp_path, config_yaml)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    import k3code.providers as providers_mod

    monkeypatch.setattr(providers_mod, "make_providers", lambda entries: [FakeProvider(steps=STEPS)])
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    return Settings(providers=[prov], default_model="default"), work


def _headless(config: Settings, mode: PermissionMode = PermissionMode.AUTO, **kw) -> dict:
    result = asyncio.run(
        cli_mod._run_headless("go", model=None, permission_mode=mode, config=config, json_output=True, **kw)
    )
    assert result is not None and "error" not in result, result
    return result


# ── the CLI entry points ──


@REFUSING
def test_headless_run_refuses_what_the_config_denies(tmp_path, monkeypatch, mode, config_yaml) -> None:
    config, work = _cli(monkeypatch, tmp_path, config_yaml)
    assert _headless(config, mode)["text"] == "finished"
    assert not (work / "ran").exists(), "k3code -p ran a command the config denies"


@REFUSING
def test_repl_refuses_what_the_config_denies(tmp_path, monkeypatch, mode, config_yaml) -> None:
    config, work = _cli(monkeypatch, tmp_path, config_yaml)
    inputs = iter(["go", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=mode, config=config))
    assert not (work / "ran").exists(), "the REPL ran a command the config denies"


def test_headless_run_without_permission_config_still_runs_the_command(tmp_path, monkeypatch) -> None:
    """The control: what the tests above refuse runs when nothing denies it."""
    config, work = _cli(monkeypatch, tmp_path)
    _headless(config, PermissionMode.YOLO)
    assert (work / "ran").exists()


@CONFIGS
def test_headless_run_applies_the_trusted_config_dir_rules(tmp_path, monkeypatch, config_yaml) -> None:
    """--config-dir names the project whose config applies; its rules apply even when the cwd is elsewhere."""
    config, work = _cli(monkeypatch, tmp_path)
    project = _project(tmp_path, config_yaml, trusted=True)
    _headless(config, project_dir=project)
    assert not (work / "ran").exists()


def test_headless_run_ignores_an_untrusted_project_allow_rule(tmp_path, monkeypatch) -> None:
    config, work = _cli(monkeypatch, tmp_path)
    project = _project(tmp_path, 'permissions:\n  bash:\n    "touch *": allow\n', trusted=False)
    _headless(config, mode=PermissionMode.DEFAULT, project_dir=project)  # default asks; headless refuses an ask
    assert not (work / "ran").exists()


def test_headless_run_applies_a_config_allow_rule(tmp_path, monkeypatch) -> None:
    """The other direction: a configured allow rule lets `-p` run what default mode would have to ask about."""
    config, work = _cli(monkeypatch, tmp_path, 'permissions:\n  bash:\n    "touch *": allow\n')
    _headless(config, mode=PermissionMode.DEFAULT)
    assert (work / "ran").exists()


# ── the state itself: whoever builds it ──


@CONFIGS
@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize("mode", list(PermissionMode))
def test_a_state_nobody_reloaded_applies_the_config(tmp_path, monkeypatch, config_yaml, mode, headless) -> None:
    _home(monkeypatch, tmp_path, config_yaml)
    state = PermissionState(mode=mode, cwd=tmp_path)
    expected = "allow" if (mode, config_yaml) == (PermissionMode.YOLO, DENY_RULE) else "deny"  # yolo: see REFUSING
    assert state.decide("bash", {"command": "touch ran"}, headless=headless).action == expected


def test_a_reload_still_replaces_the_rules(tmp_path, monkeypatch) -> None:
    home = _home(monkeypatch, tmp_path, DENY_RULE)
    state = PermissionState(mode=PermissionMode.AUTO, cwd=tmp_path)
    assert state.decide("bash", {"command": "touch ran"}).action == "deny"
    (home / "config.yaml").write_text("", encoding="utf-8")
    assert state.decide("bash", {"command": "touch ran"}).action == "deny"  # loaded once, not per call
    state.reload()
    assert state.decide("bash", {"command": "touch ran"}).action == "allow"


@CONFIGS
def test_project_dir_names_the_project_config(tmp_path, monkeypatch, config_yaml) -> None:
    _home(monkeypatch, tmp_path)
    project = _project(tmp_path, config_yaml, trusted=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state = PermissionState(mode=PermissionMode.AUTO, cwd=elsewhere, project_dir=project)
    assert state.decide("bash", {"command": "touch ran"}).action == "deny"
    assert PermissionState(mode=PermissionMode.AUTO, cwd=elsewhere).decide("bash", {"command": "touch ran"}).action == (
        "allow"
    )


@CONFIGS
def test_an_untrusted_project_config_stays_out(tmp_path, monkeypatch, config_yaml) -> None:
    _home(monkeypatch, tmp_path)
    project = _project(tmp_path, config_yaml, trusted=False)
    assert PermissionState(mode=PermissionMode.AUTO, cwd=project).decide("bash", {"command": "touch ran"}).action == (
        "allow"
    )


# ── the gateway's own loops: sub-agents ──


def _spy_children(monkeypatch) -> list:
    from k3code.subagents import runner

    built: list = []
    real_build = runner.SubagentManager.build_loop

    def spy(self, *args, **kwargs):
        loop = real_build(self, *args, **kwargs)
        built.append(loop)
        return loop

    monkeypatch.setattr(runner.SubagentManager, "build_loop", spy)
    return built


async def test_a_sub_agent_applies_the_user_config(tmp_path, monkeypatch) -> None:
    from test_autonomy_gateway import k3home, make, run_turn, start
    from test_subagents import NO_GATE, final, task_call

    built = _spy_children(monkeypatch)
    steps = [task_call("CHILD-K run it"), final("ok"), {"type": "text", "match": "CHILD-K", "text": "k"}]
    server = make(tmp_path, monkeypatch, steps, mode="auto", **NO_GATE)
    k3home(tmp_path).mkdir(parents=True, exist_ok=True)
    (k3home(tmp_path) / "config.yaml").write_text(DENY_RULE + "  hardline: ['\\bmake\\b']\n", encoding="utf-8")
    (tmp_path / "proj").mkdir()
    await start(server, tmp_path / "proj")
    await run_turn(server, "PARENT: delegate it")
    (child,) = built
    assert child.permissions.decide("bash", {"command": "touch ran"}).action == "deny"
    assert child.permissions.decide("bash", {"command": "make all"}).action == "deny"


async def test_a_worktree_sub_agent_applies_the_parents_trusted_project_config(tmp_path, monkeypatch) -> None:
    """A worktree child's cwd is a fresh path under the project: its rules come from the project the user trusted."""
    from m1cmd_helpers import git_repo
    from test_autonomy_gateway import call, make, run_turn
    from test_subagents import NO_GATE, final, task_call

    built = _spy_children(monkeypatch)
    repo = git_repo(tmp_path / "repo")
    (repo / ".k3code").mkdir()
    (repo / ".k3code" / "config.yaml").write_text(DENY_RULE, encoding="utf-8")
    steps = [
        task_call("CHILD-W run it", isolation="worktree"),
        final("ok"),
        {"type": "text", "match": "CHILD-W", "text": "w"},
    ]
    server = make(tmp_path, monkeypatch, steps, mode="auto", **NO_GATE)
    assert trust.record(repo, trusted=True)
    await call(server, "session.create", {"cwd": str(repo)})
    await run_turn(server, "PARENT")
    (child,) = built
    assert child.cwd != repo  # it did run in a worktree
    assert child.permissions.decide("bash", {"command": "touch ran"}).action == "deny"
