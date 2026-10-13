"""An answers file is checked before it is believed: typos, wrong shapes and out-of-choice values are a one-line
usage error (exit 2) with nothing written, never a traceback and never a silent default (#145)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from k3code.cli import cli
from k3code.paths import user_config_path
from k3code.setup import onboard, prompter, steps
from k3code.setup.state import state_path
from test_setup import ANSWERS

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    for name in ("K3CODE_API_KEY", "OMNIROUTE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return tmp_path


def _run(root: Path, command: str, content: Any) -> Result:
    path = root / "answers.yaml"
    path.write_text(content if isinstance(content, str) else yaml.safe_dump(content))
    args = ["setup", "--non-interactive"] if command == "setup" else ["onboard"]
    return CliRunner().invoke(cli, [*args, "--answers", str(path), "--no-probe"])


def _rejected(r: Result, *needles: str) -> None:
    """A usage error (exit 2) that names the problem, not a crash."""
    assert r.exit_code == 2, r.output
    assert isinstance(r.exception, SystemExit), repr(r.exception)  # a click exit, not an escaped exception
    assert "Traceback" not in r.output
    for needle in needles:
        assert needle in r.output, r.output


def _nothing_written(root: Path) -> None:
    assert not user_config_path().exists()
    assert not state_path().exists()
    assert not (root / "xdg" / "k3code" / "env").exists()


def test_a_typo_in_a_key_is_not_a_silent_default(root: Path) -> None:
    # the issue's repro: this used to configure claude-cli and exit 0
    r = _run(root, "onboard", "onboard:\n  mode: fast\n  provder: openai\n")
    _rejected(r, "onboard.provder", "provider")
    _nothing_written(root)


def test_a_misspelt_step_or_top_level_key_is_rejected(root: Path) -> None:
    r = _run(root, "setup", {**ANSWERS, "providres": {"entries": []}})
    _rejected(r, "providres", "providers")
    _nothing_written(root)


@pytest.mark.parametrize("command", ["setup", "onboard"])
@pytest.mark.parametrize("content", ["- a\n- b\n", "", "just text\n", "about: not-a-mapping\n"])
def test_an_answers_file_that_is_not_a_mapping_of_mappings_is_rejected(root: Path, command: str, content: str) -> None:
    _rejected(_run(root, command, content))
    _nothing_written(root)


@pytest.mark.parametrize("command", ["setup", "onboard"])
def test_malformed_yaml_is_a_message_not_a_traceback(root: Path, command: str) -> None:
    r = _run(root, command, "about: [unclosed\n  name: x: y\n")
    _rejected(r, "answers.yaml")
    assert len(r.output.strip().splitlines()) <= 5  # the usage lines and one message, no stack
    _nothing_written(root)


def test_a_bad_permission_mode_is_not_written_to_the_config(root: Path) -> None:
    # the issue's repro: this used to write permission_mode: yoloo, print "Setup complete" and exit 0
    r = _run(root, "setup", {**ANSWERS, "permissions": {"mode": "yoloo"}})
    _rejected(r, "permissions.mode", "yoloo", "ask, auto-edit, yolo")
    assert not user_config_path().exists()


def test_a_bad_usage_choice_is_a_message_not_a_keyerror(root: Path) -> None:
    r = _run(root, "setup", {**ANSWERS, "usage": {"primary": "foo"}})
    _rejected(r, "usage.primary", "foo", "coding, ops, research, mixed")
    assert not user_config_path().exists()


@pytest.mark.parametrize(
    ("section", "bad"),
    [
        ("about", {"language": "fr"}),
        ("theme", {"theme": "nonexistent"}),
        ("theme", {"keymap": "vim"}),
        ("welcome", {"mode": "sideways"}),
    ],
)
def test_every_choice_is_checked_against_its_menu(root: Path, section: str, bad: dict[str, str]) -> None:
    r = _run(root, "setup", {**ANSWERS, section: {**ANSWERS[section], **bad}})
    _rejected(r, f"{section}.{next(iter(bad))}", next(iter(bad.values())))
    assert not user_config_path().exists()


def test_a_menu_entry_can_be_answered_by_its_name_or_by_its_whole_label() -> None:
    labels = ["openai      OpenAI API key", "claude-cli  your Claude Code login"]
    for answer in ("openai", labels[0]):
        assert (
            prompter.AnswerPrompter({"onboard": {"provider": answer}}).select("onboard.provider", "?", labels) == answer
        )
    with pytest.raises(prompter.AnswersError, match="onboard.provider"):
        prompter.AnswerPrompter({"onboard": {"provider": "open"}}).select("onboard.provider", "?", labels)


@pytest.mark.parametrize(
    ("given", "want"), [(True, True), (False, False), ("true", True), ("False", False), (" TRUE ", True)]
)
def test_confirm_reads_only_true_and_false(given: Any, want: bool) -> None:
    assert prompter.AnswerPrompter({"service": {"install": given}}).confirm("service.install", "?", not want) is want


@pytest.mark.parametrize("given", ["no", "yes", "0", "1", 1, 0, "", None, [True], "maybe"])
def test_confirm_refuses_to_guess_what_is_not_a_boolean(given: Any) -> None:
    # bool("false") is True: a templated `install: "false"` used to install the systemd unit
    with pytest.raises(prompter.AnswersError, match=r"service\.install.*true or false"):
        prompter.AnswerPrompter({"service": {"install": given}}).confirm("service.install", "?", False)


def test_a_quoted_false_does_not_install_the_service(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(steps.service, "install", lambda: pytest.fail("the service must not be installed"))
    r = _run(root, "setup", {**ANSWERS, "service": {"install": "false"}})
    assert r.exit_code == 0, r.output


def test_text_takes_one_value_not_a_container() -> None:
    assert prompter.AnswerPrompter({"about": {"name": 7}}).text("about.name", "?") == "7"
    assert prompter.AnswerPrompter({"about": {"name": None}}).text("about.name", "?", "d") == ""
    with pytest.raises(prompter.AnswersError, match=r"about\.name"):
        prompter.AnswerPrompter({"about": {"name": ["a"]}}).text("about.name", "?")


@pytest.mark.parametrize(
    ("entries", "needle"),
    [
        ("oops", "providers.entries"),
        (["oops"], "providers.entries[0]"),
        ([{"preset": "anthropic", "kee": "sk-xyz"}], "kee"),
        ([{"preset": "custom", "name": "x"}], "base_url"),
        ([{"preset": "openai", "api_key_env": "bad-name!"}], "api_key_env"),
    ],
)
def test_provider_entries_are_checked_for_shape_keys_and_values(root: Path, entries: Any, needle: str) -> None:
    _rejected(_run(root, "setup", {**ANSWERS, "providers": {"entries": entries, "test": False}}), needle)
    assert not user_config_path().exists()
    assert not (root / "xdg" / "k3code" / "env").exists()  # a bad variable name never reaches the env file


def test_a_wrongly_typed_value_names_the_key(root: Path) -> None:
    r = _run(root, "setup", {**ANSWERS, "usage": {"primary": "ops", "languages": "python"}})
    _rejected(r, "usage.languages", "a list")
    _nothing_written(root)


def test_the_sample_answers_file_and_the_documented_ones_are_valid() -> None:
    prompter.validate_answers(yaml.safe_load((REPO / "install" / "answers.sample.yaml").read_text()))
    prompter.validate_answers(ANSWERS)
    prompter.validate_answers({"onboard": {"mode": "fast", "provider": "api", "endpoint": "http://h/v1", "key": "k"}})
    prompter.validate_answers(
        {"tiers": {"main": "m", "gw": {"main": "m2"}, "context_windows": {"m": "32k"}}, "project_recipes": "none"}
    )
    prompter.validate_answers({"secrets": {"NEED_KEY": "x"}, "welcome": {"accept": {"permissions": {"bash": True}}}})


#: keys only an interactive prompter is ever asked (an answers file cannot set them)
INTERACTIVE_ONLY = {
    "permissions.more",
    "permissions.rx",
    "integrations.addmcp",
    "integrations.mcp_name",
    "integrations.mcp_target",
    "onboard.retry_key",  # a rejected key is an error in an unattended run, never asked again (#147)
    "onboard.probe_failed",  # same: save anyway / retry / cancel is a question for a person
}


def test_the_declared_keys_cover_every_key_the_setup_code_reads() -> None:
    """The schema is a second list of the keys the steps read; this keeps the two from drifting apart."""
    missing: list[str] = []
    for module in (steps, onboard):
        for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
            is_read = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            if not (is_read and node.func.attr in {"select", "text", "confirm", "raw"} and node.args):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                key, dynamic = first.value, False
            elif isinstance(first, ast.JoinedStr) and isinstance(first.values[0], ast.Constant):
                key, dynamic = first.values[0].value, True  # f"providers.add{idx}": only the literal head is known
            else:
                continue
            if key not in INTERACTIVE_ONLY and not _declared(key, dynamic):
                missing.append(f"{module.__name__}:{node.lineno} {key}")
    assert not missing, missing


def _declared(key: str, dynamic: bool) -> bool:
    step, _, rest = key.partition(".")
    fields = prompter.ANSWER_KEYS.get(step)
    if fields is None:
        return False
    if not rest and not dynamic or fields is prompter.ANY:
        return True
    field = rest.split(".")[0]
    if dynamic and "." not in rest:  # a placeholder follows the head: the head is a prefix of the field name
        return (
            any(f.startswith(field) for f in fields) or "*" in fields or bool(prompter.PER_ENTRY.fullmatch(field + "0"))
        )
    return field in fields or "*" in fields or step == "providers" and bool(prompter.PER_ENTRY.fullmatch(field))
