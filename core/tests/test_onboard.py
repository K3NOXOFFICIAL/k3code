"""Onboarding: the wizard never starts on its own, `k3code onboard` writes a loadable config, the first-run
question is asked once, and headless or piped runs without a provider print one hint and exit non-zero."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from k3code import cli as cli_mod
from k3code.cli import cli
from k3code.config import load_config
from k3code.paths import user_config_path
from k3code.setup import onboard as ob
from k3code.setup import prompter as prompter_mod
from k3code.setup.prompter import AnswerPrompter


class Scripted(AnswerPrompter):
    """Answers from a mapping, records every question asked, and can simulate Ctrl+C at the first-run question."""

    interactive = True

    def __init__(self, answers: dict[str, Any], *, interrupt: bool = False) -> None:
        super().__init__(answers)
        self.asked: list[str] = []
        self.interrupt = interrupt

    def select(self, key: str, message: str, choices: list[str], default: str | None = None) -> str:
        self.asked.append(key)
        if self.interrupt and key == "onboard.mode":
            raise KeyboardInterrupt
        return super().select(key, message, choices, default)

    def text(self, key: str, message: str, default: str = "", secret: bool = False) -> str:
        self.asked.append(key)
        return super().text(key, message, default, secret)


def _boom(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("must not run here")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME, K3CODE_HOME, the XDG env file and the working directory all live under tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    for name in ("K3CODE_API_KEY", "OMNIROUTE_API_KEY", "ANTHROPIC_API_KEY"):
        # set-then-delete makes teardown restore the variable even though the flows export keys into os.environ
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return tmp_path


def _answers(root: Path, answers: dict[str, Any]) -> Path:
    path = root / "answers.yaml"
    path.write_text(yaml.safe_dump(answers))
    return path


def test_plain_noninteractive_launch_without_config_never_starts_setup(root, monkeypatch) -> None:
    monkeypatch.setattr(ob, "run_setup", _boom)
    monkeypatch.setattr(prompter_mod, "InteractivePrompter", _boom)
    r = CliRunner().invoke(cli, [])
    assert r.exit_code == 78, r.output
    lines = r.output.strip().splitlines()
    assert len(lines) == 1 and "k3code onboard" in lines[0]


def test_headless_without_config_prints_one_hint_and_never_prompts(root, monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "_run_headless", _boom)
    monkeypatch.setattr(prompter_mod, "InteractivePrompter", _boom)
    r = CliRunner().invoke(cli, ["-p", "hi"])
    assert r.exit_code == 78, r.output
    lines = r.output.strip().splitlines()
    assert len(lines) == 1 and "k3code onboard" in lines[0]


def test_onboard_fast_claude_cli_writes_a_config_that_loads(root) -> None:
    answers = _answers(root, {"onboard": {"mode": "fast", "provider": "claude-cli"}})
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    cfg = load_config(project_dir=root)
    assert [(p.name, p.kind) for p in cfg.providers] == [("claude-cli", "claude-cli")]
    assert cfg.providers[0].models == {"default": "sonnet", "strong": "opus", "cheap": "haiku", "fast": "haiku"}
    assert cfg.permission_mode == "ask"
    assert ob.question_answered()


def test_onboard_fast_api_writes_a_loadable_config_and_keeps_the_key_out_of_it(root) -> None:
    answers = _answers(
        root,
        {
            "onboard": {
                "mode": "fast",
                "provider": "api",
                "endpoint": "http://127.0.0.1:9/v1/",
                "key": "sk-SECRET-FAST",
                "model": "m-fast",
            }
        },
    )
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    assert "SECRET" not in user_config_path().read_text()
    cfg = load_config(project_dir=root)
    prov = cfg.providers[0]
    assert (prov.name, prov.kind, prov.base_url, prov.api_key_env) == (
        "endpoint",
        "openai",
        "http://127.0.0.1:9/v1",
        "K3CODE_API_KEY",
    )
    assert prov.models == {"default": "m-fast"}
    assert prov.api_key == "sk-SECRET-FAST"  # read back from the 0600 env file, as a later run would
    env_file = root / "xdg" / "k3code" / "env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_onboard_full_runs_the_wizard_and_records_the_answer(root, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ob, "run_setup", lambda p, **kw: calls.append(kw))
    answers = _answers(root, {"onboard": {"mode": "full"}})
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    assert calls == [{"do_probe": False}]
    assert ob.question_answered()


def test_onboard_without_terminal_or_answers_refuses_to_prompt(root) -> None:
    r = CliRunner().invoke(cli, ["onboard"])
    assert r.exit_code == 2 and "needs a terminal" in r.output


def test_first_run_question_is_not_repeated_after_it_is_answered(root, monkeypatch, capsys) -> None:
    monkeypatch.setattr(ob, "run_setup", _boom)
    first = Scripted({"onboard": {"mode": "fast", "provider": "claude-cli"}})
    ob.first_run(first, do_probe=False)
    assert first.asked == ["onboard.mode", "onboard.provider"]
    assert user_config_path().is_file()

    user_config_path().unlink()  # no config again, but the question was already answered
    again = Scripted({"onboard": {"mode": "fast"}})
    ob.first_run(again, do_probe=False)
    assert again.asked == []
    assert "k3code onboard" in capsys.readouterr().out


def test_first_run_full_answer_starts_the_wizard_once(root, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ob, "run_setup", lambda p, **kw: calls.append(kw))
    ob.first_run(Scripted({"onboard": {"mode": "full"}}), do_probe=False)
    assert calls == [{"do_probe": False}]
    assert ob.question_answered()


def test_interactive_launch_asks_once_and_ctrl_c_counts_as_the_answer(root, monkeypatch) -> None:
    scripted = Scripted({}, interrupt=True)
    launched: list[str] = []
    monkeypatch.setattr(ob, "run_setup", _boom)
    monkeypatch.setattr(cli_mod, "_is_interactive", lambda: True)
    monkeypatch.setattr(prompter_mod, "InteractivePrompter", lambda: scripted)
    monkeypatch.setattr(cli_mod, "_launch_tui", lambda **_kw: launched.append("tui"))

    first = CliRunner().invoke(cli, [])
    assert first.exit_code == 130
    assert scripted.asked == ["onboard.mode"] and launched == []

    second = CliRunner().invoke(cli, [])  # no config, but answered: the TUI still starts, without the question
    assert second.exit_code == 0, second.output
    assert scripted.asked == ["onboard.mode"]
    assert launched == ["tui"]


def test_fast_anthropic_uses_the_key_already_in_the_environment(root, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    p = Scripted({"onboard": {"mode": "fast", "provider": "anthropic"}})
    ob.run_fast(p, do_probe=False)
    assert "onboard.key" not in p.asked
    cfg = load_config(project_dir=root)
    prov = cfg.providers[0]
    assert (prov.name, prov.kind, prov.api_key_env) == ("anthropic", "anthropic", "ANTHROPIC_API_KEY")
    assert prov.models["default"] == "claude-sonnet-5-5" and prov.models["cheap"] == "claude-haiku-5-5"


def test_detect_prefers_what_is_already_set_up(root, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setattr(ob.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(ob.probe, "probe_url", lambda *a, **k: (False, 0.0, "down"))
    found = ob.detect_providers()
    assert set(found) == {"openrouter", "claude-cli"}
    assert ob._default_provider(found) == "openrouter"
    assert ob._default_provider({}) == "claude-cli"  # nothing set up, but the claude command exists
    monkeypatch.setattr(ob.shutil, "which", lambda name: None)
    assert ob._default_provider({}) == "api"  # no claude command either: ask for a custom endpoint


def test_fast_api_without_endpoint_is_an_error_not_a_loop(root) -> None:
    answers = _answers(root, {"onboard": {"mode": "fast", "provider": "api", "model": "m"}})
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code != 0


def test_fast_onboard_over_an_existing_chain_keeps_the_other_providers_as_fallback(root) -> None:
    """Fast setup used to replace the whole chain: adding a gateway key dropped the claude-cli fallback.

    The entry on the same endpoint is the one being replaced; the others stay, after the new one."""
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {"name": "claude-cli", "kind": "claude-cli", "models": {"default": "sonnet"}},
                    {
                        "name": "gw",
                        "kind": "openai",
                        "base_url": "http://127.0.0.1:9/v1",
                        "api_key_env": "OLD_GW_KEY",
                        "models": {"default": "old"},
                    },
                ],
                "permission_mode": "yolo",
            }
        )
    )
    answers = _answers(
        root,
        {"onboard": {"mode": "fast", "provider": "api", "endpoint": "http://127.0.0.1:9/v1", "key": "k", "model": "m"}},
    )
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    assert "kept as fallback (after endpoint): claude-cli" in r.output
    cfg = load_config(project_dir=root)
    assert [p.name for p in cfg.providers] == ["endpoint", "claude-cli"]
    assert cfg.providers[1].models == {"default": "sonnet"}
    assert cfg.permission_mode == "yolo"


def test_onboard_clears_auth_cooldowns_for_the_rewritten_provider(root) -> None:
    from k3code.router.classifier import FailoverReason
    from k3code.router.cooldown import CooldownStore

    path = root / "k3home" / "cooldowns.json"
    CooldownStore(path=path).arm(FailoverReason.auth, provider="endpoint", model="m-fast")
    answers = _answers(
        root,
        {
            "onboard": {
                "mode": "fast",
                "provider": "api",
                "endpoint": "http://127.0.0.1:9/v1/",
                "key": "sk-NEW",
                "model": "m-fast",
            }
        },
    )
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    assert CooldownStore(path=path).reason_of(provider="endpoint", model="m-fast") is None
