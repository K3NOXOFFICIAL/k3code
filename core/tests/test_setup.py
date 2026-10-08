from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from k3code.cli import cli
from k3code.setup import state as st
from k3code.setup.prompter import AnswerPrompter
from k3code.setup.steps import STEP_NAMES, build_config
from k3code.setup.wizard import run_setup

ANSWERS = {
    "welcome": {"mode": "fresh"},
    "about": {"name": "tester", "language": "de", "experience": "expert", "verbosity": "terse"},
    "system": {"confirm": True},
    "usage": {"primary": "ops", "languages": [], "frameworks": []},
    "providers": {
        "entries": [
            {
                "preset": "omniroute",
                "name": "gw",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key_env": "OMNIROUTE_API_KEY",
                "api_key": "sk-SECRET-1",
            },
            {"preset": "anthropic", "api_key": "sk-ant-SECRET-2"},
        ],
        "test": False,
    },
    "tiers": {"main": "m-main", "strong": "m-strong", "cheap": "m-cheap", "fast": "m-fast"},
    "permissions": {"mode": "ask", "extra_hardline": ["ssh \\S+ systemctl"]},
    "integrations": {
        "mcp": [{"name": "docs", "url": "http://127.0.0.1:9/mcp"}],
        "mem0_url": "",
        "skills_roots": ["/opt/skills"],
        "searxng_url": "",
    },
    "theme": {"theme": "midnight", "focus_mode": True, "keymap": "tuios"},
    "service": {"install": False},
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / ".k3code"))
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for k in ("OMNIROUTE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_non_interactive_writes_config_and_env(env: Path) -> None:
    answers = env / "answers.yaml"
    answers.write_text(yaml.safe_dump(ANSWERS))
    r = CliRunner().invoke(cli, ["setup", "--non-interactive", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    cfg_text = (env / ".k3code" / "config.yaml").read_text()
    assert "SECRET" not in cfg_text  # config only references env var names
    cfg = yaml.safe_load(cfg_text)
    assert [p["api_key_env"] for p in cfg["providers"]] == ["OMNIROUTE_API_KEY", "ANTHROPIC_API_KEY"]
    assert cfg["providers"][0]["models"] == {
        "default": "m-main",
        "strong": "m-strong",
        "cheap": "m-cheap",
        "fast": "m-fast",
    }
    assert cfg["permission_mode"] == "ask" and cfg["output_style"] == "concise"
    assert cfg["autonomy"] == {"plan_first": True, "fanout": {"max_parallel": 2}}
    assert cfg["display"] == {"theme": "midnight", "focus_mode": True}
    assert cfg["panes"]["keymap"] == "tuios"
    assert cfg["permissions"]["hardline"] == ["ssh \\S+ systemctl"]
    assert cfg["mcp"]["servers"]["docs"] == {"url": "http://127.0.0.1:9/mcp"}
    env_file = env / ".config" / "k3code" / "env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert st.read_env_file(env_file) == {"OMNIROUTE_API_KEY": "sk-SECRET-1", "ANTHROPIC_API_KEY": "sk-ant-SECRET-2"}
    # secrets must not leak anywhere else under the k3code home
    for f in (env / ".k3code").rglob("*"):
        if f.is_file():
            assert "SECRET" not in f.read_text(errors="ignore"), f
    user_md = (env / ".k3code" / "memory" / "USER.md").read_text()
    assert "tester" in user_md and "de" in user_md
    assert st.load_state().get("done")


class _Interrupt(AnswerPrompter):
    """Simulates a kill right when the providers step asks its first live question."""

    def confirm(self, key: str, message: str, default: bool = True) -> bool:
        if key == "providers.test":
            raise KeyboardInterrupt
        return super().confirm(key, message, default)


def test_resume_after_interrupt(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(KeyboardInterrupt):
        run_setup(_Interrupt(ANSWERS), do_probe=False)
    state = st.load_state()
    assert state["completed"] == STEP_NAMES[:4]  # welcome, about, system, usage
    assert json.loads(st.state_path().read_text())["data"]["about"]["name"] == "tester"
    capsys.readouterr()
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)
    out = capsys.readouterr().out
    assert "Resuming at step 'providers'" in out
    assert "== Welcome" not in out and "== About you" not in out
    assert (env / ".k3code" / "config.yaml").is_file()


def test_single_step_rerun(env: Path) -> None:
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)
    changed = {**ANSWERS, "theme": {"theme": "mono", "focus_mode": False, "keymap": "k3"}}
    run_setup(AnswerPrompter(changed), only_step="theme", do_probe=False)
    state = st.load_state()
    assert state["data"]["theme"]["theme"] == "mono"
    cfg = yaml.safe_load((env / ".k3code" / "config.yaml").read_text())
    assert cfg["display"] == {"theme": "mono", "focus_mode": False}
    assert [p["name"] for p in cfg["providers"]] == ["gw", "anthropic"]  # untouched
    assert cfg["providers"][0]["models"]["cheap"] == "m-cheap"
    assert cfg["permissions"]["hardline"] == ["ssh \\S+ systemctl"]
    # re-running tiers changes models only
    new = {**ANSWERS, "tiers": {"main": "x-main", "cheap": "x-cheap"}}
    run_setup(AnswerPrompter(new), only_step="tiers", do_probe=False)
    cfg = yaml.safe_load((env / ".k3code" / "config.yaml").read_text())
    assert cfg["providers"][0]["models"] == {"default": "x-main", "cheap": "x-cheap"}
    assert cfg["display"]["theme"] == "mono"
    with pytest.raises(ValueError):
        run_setup(AnswerPrompter({}), only_step="nope")


def test_env_file_merge_keeps_other_lines_and_mode(env: Path) -> None:
    p = st.set_env_var("A_KEY", "1")
    st.set_env_var("B_KEY", "2")
    st.set_env_var("A_KEY", "3")
    assert st.read_env_file(p) == {"A_KEY": "3", "B_KEY": "2"}
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_env_file_accepts_export_and_quoted_values(env: Path) -> None:
    """`export KEY="value"` (the shell form people paste) was read as key `export KEY` with the quotes kept."""
    p = st.env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('export A_KEY="sk-1"\nB_KEY=\'sk 2\'\nC_KEY="half\nexport D_KEY=plain\n')
    assert st.read_env_file(p) == {"A_KEY": "sk-1", "B_KEY": "sk 2", "C_KEY": '"half', "D_KEY": "plain"}
    st.set_env_var("A_KEY", "sk-3")  # replaces the export line instead of adding a second A_KEY
    assert p.read_text().count("A_KEY") == 1 and st.read_env_file(p)["A_KEY"] == "sk-3"


def test_build_config_never_contains_key_value() -> None:
    cfg = build_config(
        {"providers": {"entries": [{"name": "x", "kind": "openai", "base_url": "u", "api_key_env": "K"}]}}
    )
    assert "api_key" not in cfg["providers"][0]
    assert cfg["tiers"]["background"] == "cheap"


def test_gateway_only_warning(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = {**ANSWERS, "providers": {"entries": [ANSWERS["providers"]["entries"][0]], "test": False}}
    run_setup(AnswerPrompter(a), only_step="providers", do_probe=False)
    assert "self-hosted gateway" in capsys.readouterr().out


def test_settings_hint_and_version_flag() -> None:
    from k3code import __version__

    r = CliRunner().invoke(cli, ["--version"])
    assert r.exit_code == 0 and __version__ in r.output
    from k3code.commands.settings_cmd import render_text

    assert "k3code setup --step" in render_text(
        {
            "model_key": "d",
            "model_chain": [],
            "effort": "",
            "permission_mode": "ask",
            "permission_rules": 0,
            "focus_mode": False,
            "theme": "",
            "output_style": "",
            "providers": [],
            "paths": {},
        }
    )


def test_secrets_step_asks_only_for_missing(env: Path) -> None:
    from k3code import confio
    from k3code.paths import user_config_path

    confio.write_yaml(
        user_config_path(),
        {
            "providers": [
                {"name": "a", "kind": "openai", "base_url": "http://x/v1", "api_key_env": "HAVE_KEY"},
                {"name": "b", "kind": "openai", "base_url": "http://y/v1", "api_key_env": "NEED_KEY"},
            ]
        },
    )
    st.set_env_var("HAVE_KEY", "already")
    run_setup(AnswerPrompter({"secrets": {"NEED_KEY": "sk-NEW", "HAVE_KEY": "ignored"}}), only_step="secrets")
    assert st.read_env_file() == {"HAVE_KEY": "already", "NEED_KEY": "sk-NEW"}
    assert stat.S_IMODE(st.env_file_path().stat().st_mode) == 0o600


def test_sandbox_state_reports_each_case_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from k3code.reliability import sandbox
    from k3code.setup import detect

    monkeypatch.setattr(sandbox, "bwrap_path", lambda: None)
    assert detect.sandbox_state() == "missing"
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "usable", lambda: False)
    assert detect.sandbox_state() == "unusable"
    assert detect.sandbox_state(probe=False) == "unknown"
    monkeypatch.setattr(sandbox, "usable", lambda: True)
    assert detect.detect()["sandbox"] == "usable"

    def boom() -> bool:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(sandbox, "usable", boom)
    assert detect.sandbox_state() == "unknown"  # the wizard step goes on


_FAKE_KEY = "fake-mcp-key-for-tests-0001"  # a placeholder, never a real credential


def _integrations(answers: dict[str, object]) -> dict[str, object]:
    from k3code.setup import steps

    ctx = steps.Ctx(p=AnswerPrompter(answers), data={}, do_probe=False)
    return steps.step_integrations(ctx)


def test_no_mcp_server_is_added_by_default(env: Path) -> None:
    assert _integrations({})["mcp"] == {}


def test_hand_written_mcp_server_is_kept(env: Path) -> None:
    out = _integrations({"integrations": {"mcp": [{"name": "docs", "url": "https://mcp.example.com/mcp"}]}})
    assert out["mcp"] == {"docs": {"url": "https://mcp.example.com/mcp"}}


def test_bearer_is_read_when_connecting(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from k3code.config import McpServerConfig
    from k3code.mcpclient import request_headers

    cfg = McpServerConfig(url="https://mcp.example.com/mcp", bearer_env="EXAMPLE_MCP_KEY")
    monkeypatch.setenv("EXAMPLE_MCP_KEY", _FAKE_KEY)
    assert request_headers(cfg) == {"Authorization": f"Bearer {_FAKE_KEY}"}
    monkeypatch.delenv("EXAMPLE_MCP_KEY")
    assert request_headers(cfg) == {}  # unset: no header, and never the literal variable name


def test_provider_default_is_claude_login_when_claude_is_installed(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from k3code.setup import probe, steps

    assert "omniroute" not in probe.PRESETS and "omniroute" not in steps.PRESET_CHOICES
    monkeypatch.setattr(steps.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    assert steps.default_preset() == "claude-cli"
    ctx = steps.Ctx(p=AnswerPrompter({}), data={}, do_probe=False)
    assert steps._entry_from(ctx, 0, "primary", None) == {"name": "claude-cli", "kind": "claude-cli"}


def test_provider_default_without_claude_asks_for_a_custom_endpoint(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from k3code.setup import steps

    monkeypatch.setattr(steps.shutil, "which", lambda name: None)
    assert steps.default_preset() == "custom"
    ctx = steps.Ctx(p=AnswerPrompter({}), data={}, do_probe=False)
    assert steps._entry_from(ctx, 0, "primary", None) is None  # no default URL: nothing is invented


def test_wizard_searxng_answer_reaches_the_research_setting(env: Path) -> None:
    """The integrations answer used to land under a top-level ``searxng`` key that Settings dropped."""
    from k3code import confio
    from k3code.config import load_config
    from k3code.paths import user_config_path

    run_setup(
        AnswerPrompter({**ANSWERS, "integrations": {**ANSWERS["integrations"], "searxng_url": "http://searx.test"}}),
        do_probe=False,
    )
    assert "searxng" not in confio.read_yaml(user_config_path())
    assert load_config().research["searxng_url"] == "http://searx.test"

    # a re-run of only the integrations step keeps the user's other research keys and can clear the URL
    confio.write_yaml(
        user_config_path(),
        {
            **confio.read_yaml(user_config_path()),
            "research": {"searxng_url": "http://searx.test", "max_subquestions": 3},
        },
    )
    run_setup(
        AnswerPrompter({**ANSWERS, "integrations": {**ANSWERS["integrations"], "searxng_url": "http://new.test"}}),
        only_step="integrations",
        do_probe=False,
    )
    assert load_config().research == {"searxng_url": "http://new.test", "max_subquestions": 3}
    run_setup(
        AnswerPrompter({**ANSWERS, "integrations": {**ANSWERS["integrations"], "searxng_url": ""}}),
        only_step="integrations",
        do_probe=False,
    )
    assert load_config().research == {"max_subquestions": 3}


def test_legacy_top_level_searxng_key_is_lifted_on_load(env: Path) -> None:
    from k3code import confio
    from k3code.config import load_config
    from k3code.paths import user_config_path

    confio.write_yaml(user_config_path(), {"searxng": {"url": "http://old.test"}})
    assert load_config().research["searxng_url"] == "http://old.test"
    confio.write_yaml(
        user_config_path(), {"searxng": {"url": "http://old.test"}, "research": {"searxng_url": "http://new.test"}}
    )
    assert load_config().research["searxng_url"] == "http://new.test"
