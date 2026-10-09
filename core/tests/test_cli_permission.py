"""`k3code -p` / the REPL honour the configured permission_mode (the --permission flag used to default to "ask" and
silently override it), and a bad configured value is a usage error rather than a traceback."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from k3code import cli as cli_mod


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    modes: list[str] = []

    async def fake_headless(prompt, *, model, permission_mode, config, **kw):  # noqa: ANN001, ANN003
        modes.append(permission_mode.value)
        return {"text": "ok"}

    monkeypatch.setattr(cli_mod, "_run_headless", fake_headless)
    return modes


def invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: str,
    *args: str,
    command: object = cli_mod.main,
    prompt: bool = True,
) -> Result:
    """Run the CLI with its own home and cwd, so the developer's shell env and project config cannot leak in."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(config)
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.delenv("K3CODE_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("K3CODE_HEADLESS_PERMISSION", raising=False)
    monkeypatch.chdir(tmp_path)
    return CliRunner().invoke(command, ["-p", "hi", *args] if prompt else list(args))  # type: ignore[arg-type]


def run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, *args: str, command: object = cli_mod.main
) -> None:
    res = invoke(tmp_path, monkeypatch, config, *args, command=command)
    assert res.exit_code == 0, res.output


def assert_usage_error(res: Result, message: str) -> None:
    assert res.exit_code == 1, res.output
    assert isinstance(res.exception, SystemExit)  # click's ClickException exit, not an uncaught ValueError
    assert f"Error: {message}" in res.output
    assert "Traceback" not in res.output


PROV = "providers:\n  - {name: p, kind: claude-cli, models: {default: m}}\n"


def test_config_permission_mode_applies_to_headless(tmp_path, monkeypatch, seen):
    run(tmp_path, monkeypatch, PROV + "permission_mode: yolo\n")
    assert seen == ["yolo"]


def test_default_is_ask_without_config(tmp_path, monkeypatch, seen):
    run(tmp_path, monkeypatch, PROV)
    assert seen == ["default"]  # "ask" is the alias of PermissionMode.DEFAULT


def test_headless_permission_beats_config_and_flag_beats_both(tmp_path, monkeypatch, seen):
    cfg = PROV + "permission_mode: ask\nheadless_permission: auto-edit\n"
    run(tmp_path, monkeypatch, cfg)
    run(tmp_path, monkeypatch, cfg, "--permission", "yolo")
    assert seen == ["accept-edits", "yolo"]


def test_modes_outside_the_three_named_ones_still_apply(tmp_path, monkeypatch, seen):
    run(tmp_path, monkeypatch, PROV + "permission_mode: auto\n")
    assert seen == ["auto"]


def test_unknown_config_permission_is_a_usage_error_not_a_traceback(tmp_path, monkeypatch, seen):
    res = invoke(tmp_path, monkeypatch, PROV + "permission_mode: bypass\n")
    assert_usage_error(
        res,
        "permission_mode 'bypass' is not one of: ask, auto-edit, yolo (set in config.yaml or K3CODE_PERMISSION_MODE)",
    )
    assert seen == []


def test_unknown_headless_permission_names_its_own_key(tmp_path, monkeypatch, seen):
    res = invoke(tmp_path, monkeypatch, PROV + "headless_permission: bypass\n")
    assert_usage_error(
        res,
        "headless_permission 'bypass' is not one of: ask, auto-edit, yolo "
        "(set in config.yaml or K3CODE_HEADLESS_PERMISSION)",
    )
    assert seen == []


def test_unknown_config_permission_fails_the_repl_path_too(tmp_path, monkeypatch, seen):
    res = invoke(tmp_path, monkeypatch, PROV + "permission_mode: bypass\n", prompt=False)
    assert_usage_error(
        res,
        "permission_mode 'bypass' is not one of: ask, auto-edit, yolo (set in config.yaml or K3CODE_PERMISSION_MODE)",
    )


def test_group_entry_applies_config_permission_mode(tmp_path, monkeypatch, seen):
    """Plain `k3code -p` goes through the click group, which forwards to main with ctx.invoke. Its --permission
    default must not shadow the configured mode (testing main alone missed a group default of "ask")."""
    run(tmp_path, monkeypatch, PROV + "permission_mode: yolo\n", command=cli_mod.cli)
    assert seen == ["yolo"]


def test_group_entry_headless_permission_and_flag(tmp_path, monkeypatch, seen):
    cfg = PROV + "permission_mode: ask\nheadless_permission: auto-edit\n"
    run(tmp_path, monkeypatch, cfg, command=cli_mod.cli)
    run(tmp_path, monkeypatch, cfg, "--permission", "yolo", command=cli_mod.cli)
    assert seen == ["accept-edits", "yolo"]


async def test_bad_configured_permission_mode_fails_session_start_not_the_daemon(tmp_path, monkeypatch):
    """The gateway built sessions with a bare PermissionMode(config value): 'bypass' raised ValueError inside the
    request. Now session.create replies with an error naming the key, and the gateway keeps serving requests."""
    import json

    from m1cmd_helpers import frames_of, make_server

    server, _ = make_server(tmp_path, monkeypatch, ["ok"], permission_mode="bypass")
    await server._handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.create", "params": {"cwd": str(tmp_path)}})
    )
    reply = next(f for f in frames_of(server) if f.get("id") == 1)
    assert reply["error"]["message"].startswith(
        "InvalidPermissionMode: permission_mode 'bypass' is not one of: ask, auto-edit, yolo"
    ), reply
    await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session.list", "params": {}}))
    assert any(f.get("id") == 2 and "result" in f for f in frames_of(server))
    await server.close()


def test_headless_runs_take_auto_from_the_flag(tmp_path, monkeypatch, seen):
    """`k3code -p ... --permission auto`: only `headless_permission: auto` in the config used to reach auto mode."""
    run(tmp_path, monkeypatch, PROV, "--permission", "auto")
    run(tmp_path, monkeypatch, PROV + "headless_permission: auto-edit\n", "--permission", "auto", command=cli_mod.cli)
    assert seen == ["auto", "auto"]
    out = CliRunner().invoke(cli_mod.cli, ["--help"]).output
    assert "auto-edit, auto (" in " ".join(out.split())


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("make build", "allow"),  # would ask: auto allows it, headless or not
        ("rm -rf /", "deny"),  # hardline
        ("rm build.log", "deny"),  # a user deny rule
        ("cat .env", "deny"),  # a credential read
    ],
)
def test_headless_auto_decides_like_interactive_auto(tmp_path, command, expected):
    from k3code.permissions import Rule, decide

    rules = [Rule(tool="bash", pattern="rm *", action="deny")]
    got = [
        decide(mode="auto", tool="bash", args={"command": command}, cwd=tmp_path, user_rules=rules, headless=h).action
        for h in (True, False)
    ]
    assert got == [expected, expected]
