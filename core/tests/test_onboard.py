"""Onboarding: the wizard never starts on its own, `k3code onboard` writes a loadable config, the first-run
question is asked once, and headless or piped runs without a provider print one hint and exit non-zero."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from k3code.setup import probe
from k3code.setup import prompter as prompter_mod
from k3code.setup.prompter import AnswerPrompter
from k3code.setup.state import read_env_file, set_env_var


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


def test_onboard_fast_local_writes_a_keyless_entry_that_doctor_accepts(root, monkeypatch) -> None:
    """#107: "local (Ollama, no key)" wrote api_key_env LOCAL_API_KEY, so doctor and the first turn wanted a key."""
    from k3code.doctor import OK, check_keys

    monkeypatch.setenv("LOCAL_API_KEY", "")
    monkeypatch.delenv("LOCAL_API_KEY")
    answers = _answers(root, {"onboard": {"mode": "fast", "provider": "local", "model": "llama3"}})
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    prov = load_config(project_dir=root).providers[0]
    assert (prov.name, prov.base_url, prov.api_key_env) == ("local", "http://localhost:11434/v1", "")
    assert check_keys(load_config(project_dir=root)).status == OK
    monkeypatch.setenv("LOCAL_API_KEY", "test-local-key")  # a key already set up for the local server is kept
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    assert load_config(project_dir=root).providers[0].api_key_env == "LOCAL_API_KEY"


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


def test_fast_copies_a_key_found_only_in_the_environment_into_the_env_file(root, monkeypatch, capsys) -> None:
    """The daemon's unit and `wsl sh -lc` do not see the shell's exports; the 0600 env file is what they read."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    p = Scripted({"onboard": {"mode": "fast", "provider": "anthropic"}})
    ob.run_fast(p, do_probe=False)
    env_file = root / "xdg" / "k3code" / "env"
    assert read_env_file(env_file) == {"ANTHROPIC_API_KEY": "sk-from-env"}
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert f"stored ANTHROPIC_API_KEY in {env_file}" in out
    assert "sk-from-env" not in out


def test_fast_says_plainly_when_an_environment_only_key_is_not_copied(root, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    p = Scripted({"onboard": {"mode": "fast", "provider": "anthropic", "save_env_key": False}})
    ob.run_fast(p, do_probe=False)
    assert not (root / "xdg" / "k3code" / "env").exists()
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY stays in this shell only" in out
    assert "daemon" in out and "Windows" in out


def test_fast_leaves_a_key_that_is_already_in_the_env_file_alone(root, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-shell")
    env_file = root / "xdg" / "k3code" / "env"
    set_env_var("ANTHROPIC_API_KEY", "sk-from-file", env_file)
    p = Scripted({"onboard": {"mode": "fast", "provider": "anthropic", "save_env_key": True}})
    ob.run_fast(p, do_probe=False)
    assert read_env_file(env_file) == {"ANTHROPIC_API_KEY": "sk-from-file"}  # the file wins for the daemon
    out = capsys.readouterr().out
    assert f"using ANTHROPIC_API_KEY from the env file {env_file}" in out and "from your environment" not in out


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


def _fast_onboard_said(root: Path, cwd: Path, monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any] | None = None):
    """Fast setup run from ``cwd``; returns (what it said, the roots handed to the stack scan)."""
    from k3code.learning import projectprep, stacks

    scanned: list[Path] = []
    real_scan = stacks.scan

    def spy(path: Any, *a: Any, **k: Any) -> Any:
        scanned.append(Path(path))
        return real_scan(path, *a, **k)

    monkeypatch.setattr(stacks, "scan", spy)
    monkeypatch.setattr(projectprep, "prepare", _boom)  # `ask` mode would go through it and prompt "Apply now?"
    monkeypatch.chdir(cwd)
    p = Scripted({"onboard": {"mode": "fast", "provider": "claude-cli"}, **(extra or {})})
    said: list[str] = []
    p.say = said.append  # type: ignore[method-assign]
    monkeypatch.setattr(p, "confirm", _boom)
    ob.run_fast(p, do_probe=False)
    return said, scanned


def _project(path: Path, *, git: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    if git:
        (path / ".git").mkdir()
    return path


@pytest.mark.parametrize("where", ["home", "home_that_is_a_git_repo_subdir", "parent_of_home", "no_git", "mount"])
def test_fast_onboard_leaves_a_directory_that_is_not_a_project_alone(root, monkeypatch, where) -> None:
    """Fast setup scanned whatever the working directory was (8 s from /mnt/c/Users/<me>) and asked "Apply now?"."""
    from k3code.setup import steps

    home = _project(root / "home")  # a dotfiles repo: it has .git, it is still not a project to prepare
    cwd = {
        "home": home,
        "home_that_is_a_git_repo_subdir": home / "sub",
        "parent_of_home": _project(root, git=True),
        "no_git": _project(root / "scratch", git=False),
        "mount": _project(root / "mnt" / "c" / "proj"),
    }[where]
    cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(steps, "SLOW_MOUNTS", (root / "mnt",))
    said, scanned = _fast_onboard_said(root, cwd, monkeypatch)
    assert scanned == []
    assert any("Project recipes not offered" in s for s in said)
    assert any(s.startswith("Ready.") for s in said)


def test_fast_onboard_still_offers_the_recipes_of_a_real_project(root, monkeypatch) -> None:
    from k3code.setup import steps

    proj = _project(root / "home" / "work" / "proj")
    monkeypatch.setattr(steps, "SLOW_MOUNTS", (root / "mnt",))
    said, scanned = _fast_onboard_said(root, proj, monkeypatch, {"project_recipes": "none"})
    assert scanned == [proj.resolve()]
    assert not any("Project recipes not offered" in s for s in said)
    assert any("node (npm)" in s for s in said)


class _ModelList:
    """A ``GET /models`` answer."""

    status_code = 200

    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data

    def json(self) -> dict[str, Any]:
        return {"data": self.data}


def _serve_models(monkeypatch: pytest.MonkeyPatch, data: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(ob.probe.httpx, "get", lambda *_a, **_kw: _ModelList(data))


# OpenRouter-like: 31 lab ids that sort before the big families and were listed after them, plus non-chat ids.
LONG_LIST: list[dict[str, Any]] = [
    *({"id": f"01-lab/m-{n:02d}", "created": 1_760_000_000 + n} for n in range(30)),
    {"id": "aetherwings/mn-star", "created": 1_760_100_000},
    {"id": "openai/gpt-4o", "created": 1_715_000_000},
    {"id": "openai/gpt-5", "created": 1_754_000_000},
    {"id": "anthropic/claude-sonnet-4.5", "created": 1_759_000_000},
    {"id": "google/gemini-2.5-pro", "created": 1_750_000_000},
    {"id": "openai/gpt-4o-mini-tts", "created": 1_759_900_000},
    {"id": "openai/text-embedding-3-small", "created": 1_759_500_000},
]


def test_unattended_onboard_never_picks_a_non_chat_model_from_the_endpoint_list(root, monkeypatch) -> None:
    """#121: with no onboard.model, the default model was the alphabetically first id, babbage-002."""
    _serve_models(monkeypatch, [{"id": i} for i in ("text-embedding-3-small", "gpt-5", "babbage-002", "whisper-1")])
    answers = _answers(
        root, {"onboard": {"mode": "fast", "provider": "api", "endpoint": "http://127.0.0.1:9/v1", "key": "k"}}
    )
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers)])
    assert r.exit_code == 0, r.output
    assert "default model gpt-5." in r.output
    assert load_config(project_dir=root).providers[0].models == {"default": "gpt-5"}


class _Menus(Scripted):
    """Scripted, and keeps each menu it showed with the entry it preselected."""

    def __init__(self, answers: dict[str, Any]) -> None:
        super().__init__(answers)
        self.menus: dict[str, tuple[list[str], str | None]] = {}

    def select(self, key: str, message: str, choices: list[str], default: str | None = None) -> str:
        self.menus[key] = (choices, default)
        return super().select(key, message, choices, default)


def test_fast_model_menu_lists_current_chat_models_first(root, monkeypatch) -> None:
    """#122: the menu was the first 15 ids alphabetically ('01-ai/yi-large', ...) with '(type one)' last."""
    _serve_models(monkeypatch, LONG_LIST)
    monkeypatch.setattr(ob, "detect_providers", lambda **_: {})
    p = _Menus({"onboard": {"mode": "fast", "provider": "openrouter", "key": "k"}})
    ob.run_fast(p)
    choices, default = p.menus["onboard.model"]
    assert choices[:5] == [
        "(type one)",
        "anthropic/claude-sonnet-4.5",
        "openai/gpt-5",
        "google/gemini-2.5-pro",
        "openai/gpt-4o",
    ]
    assert choices[5] == "aetherwings/mn-star"  # then everything else, newest first
    assert len(choices) == ob.probe.MAX_MODEL_CHOICES + 1
    assert not {"openai/gpt-4o-mini-tts", "openai/text-embedding-3-small"} & set(choices)
    assert default == "anthropic/claude-sonnet-4.5"
    assert load_config(project_dir=root).providers[0].models == {"default": "anthropic/claude-sonnet-4.5"}


def test_rank_models_leaves_out_only_non_chat_ids() -> None:
    non_chat = [
        "text-embedding-3-small",
        "nomic-embed-text:latest",
        "whisper-1",
        "tts-1-hd",
        "gpt-4o-mini-tts",
        "gpt-4o-transcribe",
        "gpt-4o-audio-preview",
        "gpt-realtime",
        "dall-e-3",
        "gpt-image-1",
        "sora-2",
        "omni-moderation-latest",
        "bge-reranker-v2",
        "babbage-002",
        "davinci-002",
        "gpt-3.5-turbo-instruct",
    ]
    chat = ["mattsmodel:7b", "gpt-5", "meta-llama/llama-3.1-8b-instruct", "soraya/chat-7b", "claude-sonnet-4.5"]
    assert ob.probe.rank_models([{"id": i} for i in [*non_chat, *chat]]) == [
        "claude-sonnet-4.5",
        "gpt-5",
        "mattsmodel:7b",
        "meta-llama/llama-3.1-8b-instruct",
        "soraya/chat-7b",
    ]


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


def test_keeping_the_old_shared_key_is_not_the_default_and_the_question_says_why(root, monkeypatch) -> None:
    """Security review of #204: every endpoint used to share K3CODE_API_KEY, so the key in it may be another vendor's
    (#114), yet "Keep the K3CODE_API_KEY key?" defaulted to yes and pressing Enter kept sending it to this endpoint."""
    from k3code import confio
    from k3code.setup.state import set_env_var

    b = "http://127.0.0.1:18082/v1"
    vendor_a_key = "fake-" + "vendor-a-key"
    set_env_var("K3CODE_API_KEY", vendor_a_key)
    confio.write_yaml(
        user_config_path(),
        {"providers": [{"name": "endpoint", "kind": "openai", "base_url": b, "api_key_env": "K3CODE_API_KEY"}]},
    )
    asked: list[tuple[str, bool]] = []

    class Recording(Scripted):
        def confirm(self, key: str, message: str, default: bool = True) -> bool:
            if key == "onboard.keep_key":
                asked.append((message, default))
            return super().confirm(key, message, default)

    probes = _record_probes(monkeypatch)
    ob.run_fast(Recording({"onboard": {"mode": "fast", "provider": "api", "endpoint": b, "model": "m"}}), do_probe=True)

    [(message, default)] = asked
    assert default is False and "another endpoint" in message
    assert all(key != vendor_a_key for _url, key in probes), probes
    assert load_config(project_dir=root).providers[0].api_key != vendor_a_key


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


def test_detect_offers_claude_cli_only_while_claude_is_logged_in(root, monkeypatch, fake_claude) -> None:
    """`claude` on PATH is not a login: a logged-out one is not "found", an unknown state counts as before."""
    monkeypatch.setattr(ob.probe, "probe_url", lambda *a, **k: (False, 0.0, "down"))
    fake_claude("in")
    assert "logged in" in ob.detect_providers()["claude-cli"]
    fake_claude("out")
    assert "claude-cli" not in ob.detect_providers()
    fake_claude("old")  # a Claude Code without `auth status`: cannot tell
    assert "claude-cli" in ob.detect_providers()


def test_fast_claude_cli_says_when_claude_is_not_logged_in(root, fake_claude) -> None:
    """The unattended default (`claude` exists, no onboard.provider) used to end with a plain "Ready"."""
    fake_claude("out")
    answers = _answers(root, {"onboard": {"mode": "fast"}})
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers)])
    assert r.exit_code == 0, r.output
    assert "not logged in" in r.output and "`claude`" in r.output and "/login" in r.output
    assert [(p.name, p.kind) for p in load_config(project_dir=root).providers] == [("claude-cli", "claude-cli")]


def test_fast_claude_cli_has_no_login_note_when_logged_in_or_unprobed(root, fake_claude) -> None:
    answers = _answers(root, {"onboard": {"mode": "fast", "provider": "claude-cli"}})
    fake_claude("in")
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers)])
    assert r.exit_code == 0 and "not logged in" not in r.output, r.output
    fake_claude("out")
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])  # --no-probe: claude is not run
    assert r.exit_code == 0 and "not logged in" not in r.output, r.output


# --- a live check that rejects the key must not be waved through (#147) ---------------------------------------------

GOOD = "sk-good-" + "7" * 16  # fake keys, built at run time
BAD = "sk-bad-" + "0" * 16
REDIRECTS = {
    "/moved/models": "/v1/models",
    "/leaky/models": "http://user:" + "pw" * 6 + "@127.0.0.1/v1/models?token=" + "ab" * 8,  # credentials in the target
}


@pytest.fixture
def endpoint() -> Iterator[str]:
    """A local OpenAI-style endpoint: /v1/models lists two models for GOOD and answers 401 to any other key;
    /moved/models redirects to it; everything else is a 404. Yields its base URL (http://127.0.0.1:<port>)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/models":
                if self.headers.get("Authorization") == f"Bearer {GOOD}":
                    body = json.dumps({"data": [{"id": "m-one"}, {"id": "m-two"}]}).encode()
                    self.send_response(200)
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(401)
                    self.send_header("content-length", "0")
                    self.end_headers()
            elif self.path in ("/moved/models", "/leaky/models"):
                self.send_response(301)
                self.send_header("location", REDIRECTS[self.path])
                self.send_header("content-length", "0")
                self.end_headers()
            else:
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

        def log_message(self, *_a: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def _onboard(root: Path, base_url: str, key: str, model: str | None = None) -> Any:
    fast: dict[str, Any] = {"mode": "fast", "provider": "api", "endpoint": base_url, "key": key}
    if model:
        fast["model"] = model
    return CliRunner().invoke(cli, ["onboard", "--answers", str(_answers(root, {"onboard": fast}))])


def _env_text(root: Path) -> str:
    path = root / "xdg" / "k3code" / "env"
    return path.read_text() if path.is_file() else ""


@pytest.mark.parametrize("model", [None, "m-given"])
def test_unattended_onboard_with_a_rejected_key_fails_and_stores_nothing(root, endpoint, model) -> None:
    r = _onboard(root, f"{endpoint}/v1", BAD, model)
    assert r.exit_code != 0, r.output
    assert "K3CODE_ENDPOINT_API_KEY was rejected" in r.output and "HTTP 401" in r.output and "--no-probe" in r.output
    assert BAD not in r.output
    assert not user_config_path().exists()
    assert BAD not in _env_text(root) and "K3CODE_ENDPOINT_API_KEY" not in os.environ


def test_a_rejected_key_does_not_replace_the_one_already_stored(root, endpoint) -> None:
    assert _onboard(root, f"{endpoint}/v1", GOOD).exit_code == 0
    assert f"K3CODE_ENDPOINT_API_KEY={GOOD}" in _env_text(root)
    r = _onboard(root, f"{endpoint}/v1", BAD)
    assert r.exit_code != 0, r.output
    assert BAD not in _env_text(root) and f"K3CODE_ENDPOINT_API_KEY={GOOD}" in _env_text(root)


def test_unattended_onboard_with_an_accepted_key_picks_a_listed_model(root, endpoint) -> None:
    r = _onboard(root, f"{endpoint}/v1", f"  'K3CODE_API_KEY={GOOD}'  ")  # pasted whole, quoted
    assert r.exit_code == 0, r.output
    prov = load_config(project_dir=root).providers[0]
    assert prov.api_key == GOOD and prov.models == {"default": "m-one"}


def test_no_probe_still_stores_whatever_key_it_is_given(root, endpoint) -> None:
    answers = _answers(root, {"onboard": {"mode": "fast", "provider": "api", "endpoint": endpoint, "key": BAD}})
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    assert f"K3CODE_ENDPOINT_API_KEY={BAD}" in _env_text(root)


def test_unattended_onboard_without_a_key_does_not_fail_on_a_401(root, endpoint) -> None:
    """The key is supplied later (CI injects it at run time): nothing was rejected, so nothing fails."""
    r = _onboard(root, f"{endpoint}/v1", "")
    assert r.exit_code == 0, r.output
    assert "wants a key" in r.output and "rejected" not in r.output
    assert user_config_path().is_file()


@pytest.mark.parametrize(
    ("path", "hint"),
    [("/moved", "redirects"), ("/nope", "check the base URL")],
)
def test_a_wrong_url_is_told_apart_from_a_bad_key(root, endpoint, path, hint) -> None:
    r = _onboard(root, f"{endpoint}{path}", GOOD)
    assert r.exit_code == 0, r.output  # only a rejected key is an error; the config is still written
    assert hint in r.output and "rejected" not in r.output
    assert ("redirects to /v1/models" in r.output) == (path == "/moved")  # a redirect names where it leads


def test_an_all_capitals_key_with_padding_is_stored_as_given(root) -> None:
    key = "ABCDEFGH12345678=="
    r = _onboard(root, _closed_port_url(), key)  # unreachable: the key cannot be checked, so it is saved as typed
    assert r.exit_code == 0, r.output
    assert load_config(project_dir=root).providers[0].api_key == key


def test_a_redirect_target_is_printed_without_credentials(root, endpoint) -> None:
    r = _onboard(root, f"{endpoint}/leaky", GOOD)
    assert r.exit_code == 0, r.output
    assert "redirects to" in r.output and "pwpw" not in r.output and "abab" not in r.output


def test_an_unreachable_endpoint_says_so_and_still_writes_the_config(root) -> None:
    r = _onboard(root, _closed_port_url(), GOOD)
    assert r.exit_code == 0, r.output
    assert "could not be reached" in r.output and "rejected" not in r.output
    assert load_config(project_dir=root).providers[0].api_key == GOOD


class Retrying(Scripted):
    """Interactive: the keys typed at the re-ask and the answers to the "what now" question come from queues."""

    def __init__(
        self, answers: dict[str, Any], keys: list[str] | None = None, choices: list[str] | None = None
    ) -> None:
        super().__init__(answers)
        self.keys, self.choices = list(keys or []), list(choices or [])
        self.said: list[str] = []

    def text(self, key: str, message: str, default: str = "", secret: bool = False) -> str:
        if key == "onboard.retry_key":
            self.asked.append(key)
            return self.keys.pop(0)
        return super().text(key, message, default, secret)

    def select(self, key: str, message: str, choices: list[str], default: str | None = None) -> str:
        if key == "onboard.probe_failed":
            self.asked.append(key)
            return self.choices.pop(0)
        return super().select(key, message, choices, default)

    def say(self, text: str = "") -> None:
        self.said.append(text)


def _interactive(endpoint: str, key: str, **queues: Any) -> Retrying:
    fast = {"provider": "api", "endpoint": f"{endpoint}/v1", "key": key}
    return Retrying({"onboard": fast}, **queues)


def test_interactive_onboard_asks_for_the_key_again_and_stores_only_the_one_that_works(
    root, endpoint, monkeypatch
) -> None:
    monkeypatch.setattr(ob, "detect_providers", lambda **_: {})
    p = _interactive(endpoint, BAD, keys=[f'"{BAD}"', f"Bearer {GOOD}"])
    ob.run_fast(p)
    assert p.asked.count("onboard.retry_key") == 2 and "onboard.probe_failed" not in p.asked
    said = "\n".join(p.said)
    assert f"{len(BAD)} characters, ends in {BAD[-4:]}" in said and BAD not in said  # enough to spot a typo
    prov = load_config(project_dir=root).providers[0]
    assert prov.api_key == GOOD and prov.models == {"default": "m-one"}
    assert BAD not in _env_text(root)


def test_interactive_onboard_gives_up_after_three_rejected_keys(root, endpoint, monkeypatch) -> None:
    monkeypatch.setattr(ob, "detect_providers", lambda **_: {})
    p = _interactive(endpoint, BAD, keys=[BAD, BAD], choices=["cancel"])
    with pytest.raises(KeyboardInterrupt):
        ob.run_fast(p)
    assert p.asked.count("onboard.retry_key") == 2  # the first key plus two more is three tries
    assert not user_config_path().exists() and BAD not in _env_text(root)


def test_interactive_save_anyway_after_the_tries_stores_the_key_as_asked(root, endpoint, monkeypatch) -> None:
    monkeypatch.setattr(ob, "detect_providers", lambda **_: {})
    p = _interactive(endpoint, BAD, keys=[BAD, BAD], choices=["save anyway"])
    ob.run_fast(p)
    assert load_config(project_dir=root).providers[0].api_key == BAD


def test_interactive_network_failure_offers_retry_save_or_cancel(root, monkeypatch) -> None:
    monkeypatch.setattr(ob, "detect_providers", lambda **_: {})
    p = Retrying(
        {"onboard": {"provider": "api", "endpoint": _closed_port_url(), "key": GOOD}}, choices=["retry", "save anyway"]
    )
    ob.run_fast(p)
    assert p.asked.count("onboard.probe_failed") == 2
    assert load_config(project_dir=root).providers[0].api_key == GOOD


@pytest.mark.parametrize(
    ("pasted", "key"),
    [
        (f"  {GOOD}\n", GOOD),
        (f'"{GOOD}"', GOOD),
        (f"'{GOOD}'", GOOD),
        (f"K3CODE_API_KEY={GOOD}", GOOD),
        (f'export ANTHROPIC_API_KEY="{GOOD}"', GOOD),
        (f"Bearer {GOOD}", GOOD),
        (f"bearer  {GOOD}", GOOD),
        ("sk-ant-abc=def", "sk-ant-abc=def"),  # an "=" inside a key is not a NAME= prefix
        ("sk-" + "A" * 20 + "==", "sk-" + "A" * 20 + "=="),
        ("ABCD1234=", "ABCD1234="),  # all capitals and digits, padded: still a key, not an empty NAME=
        ("ABCDEFGH12345678==", "ABCDEFGH12345678=="),
        ('"ABCD1234="', "ABCD1234="),
        ("", ""),
    ],
)
def test_clean_key_takes_the_key_out_of_what_was_pasted(pasted, key) -> None:
    assert ob.clean_key(pasted) == key


@pytest.mark.parametrize(
    ("detail", "kind"),
    [
        ("HTTP 401", "auth"),
        ("HTTP 403", "auth"),
        ("HTTP 301", "redirect"),
        ("HTTP 308, redirects to /v1/models", "redirect"),
        ("HTTP 404", "url"),
        ("UnsupportedProtocol", "url"),
        ("ConnectError", "unreachable"),
        ("ReadTimeout", "unreachable"),
        ("HTTP 429", "other"),
        ("HTTP 502", "other"),
    ],
)
def test_probe_failures_are_classified(detail, kind) -> None:
    assert probe.explain(detail)[0] == kind


def test_list_models_names_where_a_redirect_goes(endpoint) -> None:
    ok, _ms, _ids, detail = probe.list_models({"kind": "openai", "base_url": f"{endpoint}/moved"}, GOOD)
    assert not ok and detail == "HTTP 301, redirects to /v1/models"
