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

    def confirm(self, key: str, message: str, default: bool = True) -> bool:
        self.asked.append(key)
        return super().confirm(key, message, default)


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
    for name in (
        "K3CODE_API_KEY",
        "K3CODE_ENDPOINT_API_KEY",
        "K3CODE_ENDPOINT_2_API_KEY",
        "OMNIROUTE_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
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
        "K3CODE_ENDPOINT_API_KEY",  # its own variable, not a K3CODE_API_KEY shared by every endpoint (#114, #120)
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


def _fast_api(root: Path, endpoint: str, **extra: str) -> str:
    answers = _answers(
        root, {"onboard": {"mode": "fast", "provider": "api", "endpoint": endpoint, "model": "m", **extra}}
    )
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    return r.output


def test_fast_api_does_not_send_a_leftover_generic_key_to_a_new_endpoint(root) -> None:
    """#114: K3CODE_API_KEY left in the env file by another vendor was used for the new endpoint without a question
    (and called "from your environment"), so the old vendor's key went to the new host."""
    from k3code.setup.state import set_env_var

    old = "fake-" + "old-vendor-key"
    set_env_var("K3CODE_API_KEY", old)
    out = _fast_api(root, "http://127.0.0.1:18081/v1")
    assert "using K3CODE_API_KEY" not in out
    prov = load_config(project_dir=root).providers[0]
    assert prov.api_key_env != "K3CODE_API_KEY"
    assert prov.api_key != old
    assert f"NOTE: {prov.api_key_env} is not set yet" in out


def test_fast_offers_to_keep_or_replace_a_stored_key_and_says_where_it_is(root, capsys) -> None:
    """#114: a key already stored for the provider was used without a word, so a wrong stored key could not be
    replaced in fast setup; the message also said "your environment" for a key from the env file."""
    from k3code.setup.state import set_env_var

    set_env_var("ANTHROPIC_API_KEY", "fake-" + "wrong-key")
    keep = Scripted({"onboard": {"mode": "fast", "provider": "anthropic"}})
    ob.run_fast(keep, do_probe=False)
    assert "onboard.keep_key" in keep.asked and "onboard.key" not in keep.asked
    assert "using ANTHROPIC_API_KEY from the env file" in capsys.readouterr().out

    replace = Scripted({"onboard": {"mode": "fast", "provider": "anthropic", "keep_key": False, "key": "fake-right"}})
    ob.run_fast(replace, do_probe=False)
    assert "onboard.key" in replace.asked
    assert load_config(project_dir=root).providers[0].api_key == "fake-right"


def test_fast_api_asks_for_the_key_of_a_new_endpoint_even_with_a_generic_key_set(root) -> None:
    """#114, interactive: the key question was skipped whenever K3CODE_API_KEY had any value."""
    from k3code.setup.state import set_env_var

    set_env_var("K3CODE_API_KEY", "fake-" + "old-vendor-key")
    endpoint = "http://127.0.0.1:18081/v1"
    p = Scripted({"onboard": {"mode": "fast", "provider": "api", "endpoint": endpoint, "key": "fake-new-key"}})
    ob.run_fast(p, do_probe=False)
    assert "onboard.key" in p.asked
    assert load_config(project_dir=root).providers[0].api_key == "fake-new-key"


def test_fast_onboarding_of_a_second_endpoint_keeps_the_first_with_its_own_key(root) -> None:
    """#120: every fast custom endpoint was named "endpoint" with key variable K3CODE_API_KEY, so setting up a
    second gateway dropped the first entry and overwrote its key in the env file."""
    _fast_api(root, "http://127.0.0.1:18081/v1", key="fake-key-a")
    out = _fast_api(root, "http://127.0.0.1:18082/v1", key="fake-key-b")
    assert "kept as fallback" in out
    cfg = load_config(project_dir=root)
    assert [(p.base_url, p.api_key) for p in cfg.providers] == [
        ("http://127.0.0.1:18082/v1", "fake-key-b"),
        ("http://127.0.0.1:18081/v1", "fake-key-a"),
    ]
    assert len({p.name for p in cfg.providers}) == 2 and len({p.api_key_env for p in cfg.providers}) == 2

    # a re-run for the first endpoint without a key replaces that entry and keeps its own key
    _fast_api(root, "http://127.0.0.1:18081/v1")
    cfg = load_config(project_dir=root)
    assert [(p.base_url, p.api_key) for p in cfg.providers] == [
        ("http://127.0.0.1:18081/v1", "fake-key-a"),
        ("http://127.0.0.1:18082/v1", "fake-key-b"),
    ]


def _record_probes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every (base_url, key) fast setup lists models with; the answer is a plain failure."""
    seen: list[tuple[str, str]] = []

    def fake(entry: dict[str, Any], key: str, timeout: float = 8.0) -> tuple[bool, float, list[str], str]:
        seen.append((entry["base_url"], key))
        return False, 0.0, [], "HTTP 401"

    monkeypatch.setattr(ob.probe, "list_models", fake)
    return seen


def test_declining_the_stored_key_and_giving_none_never_sends_that_key_to_the_endpoint(
    root, monkeypatch, capsys
) -> None:
    """Security review of #204: after "n" and a blank key the entry still used the declined variable, so vendor A's
    key (left on the shared K3CODE_API_KEY by the old code) kept going to endpoint B, and B was probed with it."""
    from k3code import confio
    from k3code.setup.state import set_env_var

    b = "http://127.0.0.1:18082/v1"
    vendor_a_key = "fake-" + "vendor-a-key"
    set_env_var("K3CODE_API_KEY", vendor_a_key)
    confio.write_yaml(
        user_config_path(),
        {"providers": [{"name": "endpoint", "kind": "openai", "base_url": b, "api_key_env": "K3CODE_API_KEY"}]},
    )
    probes = _record_probes(monkeypatch)
    p = Scripted({"onboard": {"mode": "fast", "provider": "api", "endpoint": b, "keep_key": False, "model": "m"}})
    ob.run_fast(p, do_probe=True)

    assert "onboard.keep_key" in p.asked
    assert all(key != vendor_a_key for _url, key in probes), probes
    prov = load_config(project_dir=root).providers[0]
    assert prov.api_key_env != "K3CODE_API_KEY" and prov.api_key != vendor_a_key
    assert f"NOTE: {prov.api_key_env} is not set yet" in capsys.readouterr().out


def test_declining_a_vendor_key_and_giving_none_does_not_probe_with_it_and_says_it_is_still_stored(
    root, monkeypatch, capsys
) -> None:
    """The same for a preset: its variable is fixed, so the old key stays there, but it is not used to probe and the
    output no longer claims nothing is stored."""
    from k3code.setup.state import set_env_var

    old = "fake-" + "wrong-anthropic-key"
    set_env_var("ANTHROPIC_API_KEY", old)
    probes = _record_probes(monkeypatch)
    ob.run_fast(Scripted({"onboard": {"mode": "fast", "provider": "anthropic", "keep_key": False}}), do_probe=True)

    assert probes == []
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY" in out and "still stored" in out and "not set yet" not in out
    assert old not in out


def test_fast_api_never_takes_over_a_vendor_key_variable_for_another_url(root, monkeypatch) -> None:
    """The old wizard wrote an openai entry with an edited URL on OPENAI_API_KEY; fast setup for that URL passed the
    variable on, so the OpenAI key kept going to the other host."""
    from k3code import confio

    local = "http://127.0.0.1:18083/v1"
    openai_key = "fake-" + "openai-key"
    monkeypatch.setenv("OPENAI_API_KEY", openai_key)
    confio.write_yaml(
        user_config_path(),
        {"providers": [{"name": "openai", "kind": "openai", "base_url": local, "api_key_env": "OPENAI_API_KEY"}]},
    )
    _fast_api(root, local)
    prov = load_config(project_dir=root).providers[0]
    assert prov.base_url == local
    assert prov.api_key_env != "OPENAI_API_KEY" and prov.api_key != openai_key
