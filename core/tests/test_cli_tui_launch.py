"""The default interactive path (`k3code` on a terminal) hands --permission and --config-dir to the TUI's gateway:
it used to drop both, so `k3code --permission ask` ran in the config's yolo and the gateway applied the cwd's project
config although trust had been offered for --config-dir."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from k3code import cli as cli_mod
from k3code import trust

PROV = "providers:\n  - {name: p, kind: claude-cli, models: {default: m}}\n"


@pytest.fixture
def launched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(PROV + "permission_mode: yolo\n")
    monkeypatch.setenv("K3CODE_HOME", str(home))
    for var in ("K3CODE_PERMISSION_MODE", "K3CODE_PROJECT_DIR", "K3CODE_MAX_TURNS"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "root"
    (root / "tui" / "dist").mkdir(parents=True)
    (root / "tui" / "dist" / "entry.js").write_text("")
    import k3code.paths as paths_mod

    monkeypatch.setattr(paths_mod, "find_node", lambda: "node")
    monkeypatch.setattr(cli_mod, "_find_repo_root", lambda: root)
    monkeypatch.setattr(cli_mod, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli_mod, "_offer_project_trust", lambda project_dir: None)
    envs: list[dict[str, str]] = []
    monkeypatch.setattr(
        cli_mod.subprocess, "run", lambda argv, env, check: envs.append(env) or SimpleNamespace(returncode=0)
    )
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return envs


def test_permission_flag_reaches_the_tui_gateway(launched):
    res = CliRunner().invoke(cli_mod.main, ["--permission", "ask"])
    assert res.exit_code == 0, res.output
    assert launched[0]["K3CODE_PERMISSION_MODE"] == "default"


def test_without_the_flag_the_config_mode_is_left_to_the_gateway(launched):
    res = CliRunner().invoke(cli_mod.main, [])
    assert res.exit_code == 0, res.output
    assert "K3CODE_PERMISSION_MODE" not in launched[0]


def test_config_dir_reaches_the_tui_gateway(launched, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    res = CliRunner().invoke(cli_mod.main, ["--config-dir", str(proj)])
    assert res.exit_code == 0, res.output
    assert Path(launched[0]["K3CODE_PROJECT_DIR"]) == proj.resolve()


def test_agents_opens_the_tui_on_the_daemon_with_the_agent_view(launched, monkeypatch):
    monkeypatch.delenv("K3CODE_TUI_VIEW", raising=False)
    monkeypatch.setenv("K3CODE_TUI_RESUME", "stale-session")  # inherited from a parent k3code: must not resume it
    res = CliRunner().invoke(cli_mod.cli, ["agents"])
    assert res.exit_code == 0, res.output
    env = launched[0]
    assert env["K3CODE_TUI_VIEW"] == "agents"
    assert env["K3CODE_TUI_RESUME"] == ""
    assert env["K3CODE_GATEWAY_CMD"].endswith("-m k3code.cli gateway --attach")


def test_agents_sends_the_shell_cwd_as_the_session_workspace(launched, tmp_path, monkeypatch):
    """The TUI forges its startup session on the daemon, whose own cwd is wherever it was started."""
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli_mod.cli, ["agents"])
    assert res.exit_code == 0, res.output
    assert launched[0]["K3CODE_TUI_CWD"] == os.getcwd()


async def test_gateway_loads_the_project_config_named_by_k3code_project_dir(tmp_path, monkeypatch):
    from k3code.gateway.server import GatewayServer
    from k3code.gateway.sessions import SessionStore

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    (proj / ".k3code").mkdir(parents=True)
    (proj / ".k3code" / "config.yaml").write_text("max_turns: 7\n")
    trust.record(proj, trusted=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("K3CODE_PROJECT_DIR", str(proj))
    server = GatewayServer(store=SessionStore(tmp_path / "s.db"))
    try:
        assert server.config.max_turns == 7
    finally:
        await server.close()
