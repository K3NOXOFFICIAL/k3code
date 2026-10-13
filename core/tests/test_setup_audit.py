"""Setup audit fixes: a single-step re-run keeps the config it never asked about; answers are read strictly."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from k3code import confio, service
from k3code.cli import cli
from k3code.paths import user_config_path
from k3code.setup import state as st
from k3code.setup.prompter import AnswerPrompter
from k3code.setup.wizard import run_setup


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / ".k3code"))
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("K3CODE_PROJECT_DIR", str(tmp_path))
    return tmp_path


def _seed() -> dict:
    """A config the user has tuned by hand: rules, a hardline, TUI keys, extra autonomy/MCP/mem0 keys."""
    cfg = {
        "providers": [
            {
                "name": "gw",
                "kind": "openai",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key_env": "GW_KEY",
                "models": {"default": "m1"},
            }
        ],
        "permission_mode": "yolo",
        "permissions": {"bash": {"git push*": "deny", "make *": "allow"}, "hardline": ["foo"]},
        "autonomy": {"plan_first": False, "max_tool_errors": 4, "fanout": {"max_parallel": 9, "other": 1}},
        "display": {"theme": "midnight", "focus_mode": True, "tui_pet": True},
        "panes": {"keymap": "tuios", "extra": 1},
        "mcp": {"servers": {"a": {"url": "http://127.0.0.1:9/mcp"}}},
        "skills": {"roots": ["/opt/skills"]},
        "mem0": {"url": "http://mem.test", "api_key_env": "MEM_KEY", "user_id": "me"},
        "research": {"searxng_url": "http://searx.test", "max_subquestions": 3},
        "output_style": "explanatory",
    }
    confio.write_yaml(user_config_path(), cfg)
    return cfg


def _cfg() -> dict:
    return confio.read_yaml(user_config_path())


# ── H3 ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_step_permissions_keeps_mode_and_rules(env: Path) -> None:
    seed = _seed()
    run_setup(AnswerPrompter({"permissions": {"extra_hardline": ["bar"]}}), only_step="permissions", do_probe=False)
    cfg = _cfg()
    assert cfg["permission_mode"] == "yolo"  # no answer for the mode: the current one is the default
    assert cfg["permissions"]["bash"] == seed["permissions"]["bash"]
    assert cfg["permissions"]["hardline"] == ["foo", "bar"]
    run_setup(AnswerPrompter({"permissions": {"mode": "ask"}}), only_step="permissions", do_probe=False)
    cfg = _cfg()
    assert cfg["permission_mode"] == "ask" and cfg["permissions"]["hardline"] == ["foo", "bar"]
    assert cfg["permissions"]["bash"] == seed["permissions"]["bash"]


def test_step_usage_keeps_other_autonomy_keys(env: Path) -> None:
    _seed()
    run_setup(AnswerPrompter({"usage": {"primary": "ops"}}), only_step="usage", do_probe=False)
    cfg = _cfg()
    assert cfg["output_style"] == "concise"
    assert cfg["autonomy"]["plan_first"] is True and cfg["autonomy"]["max_tool_errors"] == 4
    assert cfg["autonomy"]["fanout"] == {"max_parallel": 2, "other": 1}
    assert cfg["permission_mode"] == "yolo"  # `usage` does not own it


def test_step_theme_keeps_tui_keys(env: Path) -> None:
    _seed()
    run_setup(AnswerPrompter({"theme": {"theme": "mono"}}), only_step="theme", do_probe=False)
    cfg = _cfg()
    assert cfg["display"] == {"theme": "mono", "focus_mode": True, "tui_pet": True}
    assert cfg["panes"] == {"keymap": "tuios", "extra": 1}


def test_step_integrations_keeps_mcp_skills_mem0(env: Path) -> None:
    seed = _seed()
    answers = {"integrations": {"mcp": [{"name": "b", "url": "http://127.0.0.1:9/b"}]}}
    run_setup(AnswerPrompter(answers), only_step="integrations", do_probe=False)
    cfg = _cfg()
    assert set(cfg["mcp"]["servers"]) == {"a", "b"}
    assert cfg["skills"] == {"roots": ["/opt/skills"]}
    assert cfg["mem0"]["url"] == "http://mem.test" and cfg["research"] == seed["research"]
    # an explicit blank clears just that URL
    run_setup(AnswerPrompter({"integrations": {"mem0_url": ""}}), only_step="integrations", do_probe=False)
    cfg = _cfg()
    assert cfg["mem0"] == {"api_key_env": "MEM_KEY", "user_id": "me"} and "b" in cfg["mcp"]["servers"]


def test_an_answers_file_that_lists_no_servers_or_roots_clears_them_and_a_silent_one_keeps_them(env: Path) -> None:
    _seed()
    run_setup(AnswerPrompter({"integrations": {}}), only_step="integrations", do_probe=False)
    cfg = _cfg()
    assert set(cfg["mcp"]["servers"]) == {"a"} and cfg["skills"] == {"roots": ["/opt/skills"]}  # no key: no change
    run_setup(
        AnswerPrompter({"integrations": {"mcp": [], "skills_roots": []}}), only_step="integrations", do_probe=False
    )
    cfg = _cfg()
    assert "mcp" not in cfg and "skills" not in cfg  # an explicit empty list is the request to remove them
    assert cfg["mem0"]["url"] == "http://mem.test"  # and nothing else of the integrations moves


def test_the_hardline_only_grows_whatever_a_rerun_answers(env: Path) -> None:
    _seed()
    for answers in ({"extra_hardline": []}, {"extra_hardline": ["foo", "baz"]}, {}):
        run_setup(AnswerPrompter({"permissions": answers}), only_step="permissions", do_probe=False)
    assert _cfg()["permissions"]["hardline"] == ["foo", "baz"]


# ── H4 ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_answers_file_no_does_not_install_the_service(env: Path, monkeypatch) -> None:
    """bool("no") was True. A quoted "false" is read as false; "no" is no boolean and is refused, not guessed at."""
    calls: list[str] = []
    monkeypatch.setattr(service, "install", lambda **_: calls.append("install") or ["done"])
    answers = env / "a.yaml"
    answers.write_text('service: {install: "false"}\ntheme: {focus_mode: "false", theme: mono}\n')
    r = CliRunner().invoke(cli, ["setup", "--non-interactive", "--answers", str(answers), "--step", "service"])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(cli, ["setup", "--non-interactive", "--answers", str(answers), "--step", "theme"])
    assert r.exit_code == 0, r.output
    assert calls == [] and _cfg()["display"]["focus_mode"] is False
    answers.write_text('service: {install: "no"}\n')
    r = CliRunner().invoke(cli, ["setup", "--non-interactive", "--answers", str(answers), "--step", "service"])
    assert r.exit_code != 0 and "service.install" in r.output and "Traceback" not in r.output
    assert calls == []


def test_service_error_is_a_failed_step_not_a_traceback(env: Path, monkeypatch) -> None:
    def boom(**_kw):
        raise service.ServiceError("systemd is not available")

    monkeypatch.setattr(service, "install", boom)
    answers = env / "a.yaml"
    answers.write_text("service: {install: true}\n")
    r = CliRunner().invoke(cli, ["setup", "--non-interactive", "--answers", str(answers), "--step", "service"])
    assert r.exit_code == 0 and "systemd is not available" in r.output, r.output
    assert st.load_state()["data"]["service"]["error"] == "systemd is not available"


def test_invalid_yes_no_answer_is_a_cli_error(env: Path) -> None:
    answers = env / "a.yaml"
    answers.write_text("theme: {focus_mode: maybe}\n")
    r = CliRunner().invoke(cli, ["setup", "--non-interactive", "--answers", str(answers), "--step", "theme"])
    assert r.exit_code == 2 and "theme.focus_mode" in r.output and "Traceback" not in r.output  # a usage error


# ── L6 ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_answers_file_keeps_a_fourth_provider(env: Path) -> None:
    entries = [
        {"name": f"p{i}", "base_url": f"http://127.0.0.1:{9 + i}/v1", "api_key_env": f"P{i}_KEY", "preset": "custom"}
        for i in range(4)
    ]
    run_setup(AnswerPrompter({"providers": {"entries": entries, "test": False}}), only_step="providers", do_probe=False)
    assert [p["name"] for p in _cfg()["providers"]] == ["p0", "p1", "p2", "p3"]


@pytest.mark.parametrize("content", ["a: [unclosed\n", "- just\n- a list\n", "42\n"])
def test_bad_answers_file_is_a_cli_error(env: Path, content: str) -> None:
    answers = env / "a.yaml"
    answers.write_text(content)
    for args in (["setup", "--non-interactive", "--answers", str(answers)], ["onboard", "--answers", str(answers)]):
        r = CliRunner().invoke(cli, args)
        assert r.exit_code == 2 and "Traceback" not in r.output and str(answers) in r.output, (args, r.output)  # usage
        assert not isinstance(r.exception, (yaml.YAMLError, AttributeError, TypeError)), r.exception


def test_env_names_from_an_answers_file_cannot_inject_lines(env: Path) -> None:
    secret = "sk-" + "audit" + "0123456789abcdef"  # built at run time: no secret-shaped literal in the source
    bad_name = "GOOD_KEY\nINJECTED=1"
    entry = {"name": "x", "base_url": "http://127.0.0.1:9/v1", "api_key_env": bad_name, "api_key": secret}
    with pytest.raises(ValueError, match="api_key_env"):
        run_setup(
            AnswerPrompter({"providers": {"entries": [entry], "test": False}}), only_step="providers", do_probe=False
        )
    assert not st.env_file_path().exists()
    with pytest.raises(ValueError, match="line break"):
        st.set_env_var("GOOD_KEY", "a\nINJECTED=1")
    with pytest.raises(ValueError, match="variable name"):
        st.set_env_var("BAD KEY", "v")
    with pytest.raises(ValueError, match="provider name"):
        run_setup(
            AnswerPrompter({"providers": {"entries": [{**entry, "api_key_env": "OK_KEY", "name": "a\nb"}]}}),
            only_step="providers",
            do_probe=False,
        )
