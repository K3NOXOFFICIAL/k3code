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
    assert "K3CODE_API_KEY was rejected" in r.output and "HTTP 401" in r.output and "--no-probe" in r.output
    assert BAD not in r.output
    assert not user_config_path().exists()
    assert BAD not in _env_text(root) and "K3CODE_API_KEY" not in os.environ


def test_a_rejected_key_does_not_replace_the_one_already_stored(root, endpoint) -> None:
    assert _onboard(root, f"{endpoint}/v1", GOOD).exit_code == 0
    assert f"K3CODE_API_KEY={GOOD}" in _env_text(root)
    r = _onboard(root, f"{endpoint}/v1", BAD)
    assert r.exit_code != 0, r.output
    assert BAD not in _env_text(root) and f"K3CODE_API_KEY={GOOD}" in _env_text(root)


def test_unattended_onboard_with_an_accepted_key_picks_a_listed_model(root, endpoint) -> None:
    r = _onboard(root, f"{endpoint}/v1", f"  'K3CODE_API_KEY={GOOD}'  ")  # pasted whole, quoted
    assert r.exit_code == 0, r.output
    prov = load_config(project_dir=root).providers[0]
    assert prov.api_key == GOOD and prov.models == {"default": "m-one"}


def test_no_probe_still_stores_whatever_key_it_is_given(root, endpoint) -> None:
    answers = _answers(root, {"onboard": {"mode": "fast", "provider": "api", "endpoint": endpoint, "key": BAD}})
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(answers), "--no-probe"])
    assert r.exit_code == 0, r.output
    assert f"K3CODE_API_KEY={BAD}" in _env_text(root)


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
    monkeypatch.setattr(ob, "detect_providers", lambda: {})
    p = _interactive(endpoint, BAD, keys=[f'"{BAD}"', f"Bearer {GOOD}"])
    ob.run_fast(p)
    assert p.asked.count("onboard.retry_key") == 2 and "onboard.probe_failed" not in p.asked
    said = "\n".join(p.said)
    assert f"{len(BAD)} characters, ends in {BAD[-4:]}" in said and BAD not in said  # enough to spot a typo
    prov = load_config(project_dir=root).providers[0]
    assert prov.api_key == GOOD and prov.models == {"default": "m-one"}
    assert BAD not in _env_text(root)


def test_interactive_onboard_gives_up_after_three_rejected_keys(root, endpoint, monkeypatch) -> None:
    monkeypatch.setattr(ob, "detect_providers", lambda: {})
    p = _interactive(endpoint, BAD, keys=[BAD, BAD], choices=["cancel"])
    with pytest.raises(KeyboardInterrupt):
        ob.run_fast(p)
    assert p.asked.count("onboard.retry_key") == 2  # the first key plus two more is three tries
    assert not user_config_path().exists() and BAD not in _env_text(root)


def test_interactive_save_anyway_after_the_tries_stores_the_key_as_asked(root, endpoint, monkeypatch) -> None:
    monkeypatch.setattr(ob, "detect_providers", lambda: {})
    p = _interactive(endpoint, BAD, keys=[BAD, BAD], choices=["save anyway"])
    ob.run_fast(p)
    assert load_config(project_dir=root).providers[0].api_key == BAD


def test_interactive_network_failure_offers_retry_save_or_cancel(root, monkeypatch) -> None:
    monkeypatch.setattr(ob, "detect_providers", lambda: {})
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
