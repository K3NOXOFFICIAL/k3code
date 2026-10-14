"""A malformed ``permissions:`` entry fails closed: it never drops the file's other rules (deny rules included)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from k3code import doctor
from k3code.permissions import PermissionMode, rules
from k3code.permissions.state import UNREADABLE, PermissionState, load_permissions_config, permissions_from_text

#: YAML spellings of an action that is not exactly allow|ask|deny (``no`` is a bool, an empty value is null).
BAD_ACTIONS = ["Deny", '"deny "', "block", "no", "", "[deny]", "{a: deny}", "1"]
#: Tool entries that are neither an action nor a pattern→action mapping.
BAD_TOOL_ENTRIES = ['["git push *"]', "3", "true"]


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _state(tmp_path: Path, body: str, mode: PermissionMode) -> PermissionState:
    st = PermissionState(mode=mode, cwd=tmp_path, user_config=_config(tmp_path, body))
    st.reload()
    return st


def _bash(st: PermissionState, command: str) -> str:
    return st.decide("bash", {"command": command}).action


@pytest.mark.parametrize("bad", BAD_ACTIONS)
def test_an_invalid_action_keeps_every_other_rule_and_is_itself_a_deny(tmp_path, bad):
    path = _config(tmp_path, f'permissions:\n  bash:\n    "git push *": deny\n    "rm -rf *": {bad}\n')
    parsed, _ = load_permissions_config(path)
    assert rules.evaluate("bash", "git push origin main", parsed).action == "deny"
    assert rules.evaluate("bash", "rm -rf build", parsed).action == "deny"


@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.AUTO])
@pytest.mark.parametrize("bad", BAD_ACTIONS)
def test_default_and_auto_still_deny_what_the_user_denied(tmp_path, bad, mode):
    st = _state(tmp_path, f'permissions:\n  bash:\n    "git push *": deny\n    "rm -rf *": {bad}\n', mode)
    assert _bash(st, "git push origin main") == "deny"
    assert _bash(st, "rm -rf build") == "deny"


@pytest.mark.parametrize("entry", BAD_TOOL_ENTRIES)
def test_a_tool_entry_that_is_not_an_action_or_mapping_denies_that_tool_only(tmp_path, entry):
    st = _state(tmp_path, f'permissions:\n  bash: {entry}\n  edit:\n    "/etc/**": deny\n', PermissionMode.AUTO)
    assert _bash(st, "git push origin main") == "deny"
    assert st.decide("edit", {"path": "/etc/hosts"}).action == "deny"
    assert st.decide("read", {"path": str(tmp_path / "x")}).action == "allow"


def test_a_pattern_that_is_not_text_denies_its_tool_and_keeps_the_rest(tmp_path):
    st = _state(tmp_path, 'permissions:\n  bash:\n    2024: allow\n  edit:\n    "/etc/**": deny\n', PermissionMode.AUTO)
    assert _bash(st, "git push origin main") == "deny"
    assert st.decide("edit", {"path": "/etc/hosts"}).action == "deny"


def test_a_tool_name_that_is_not_text_does_not_crash_or_drop_the_rest(tmp_path):
    st = _state(tmp_path, 'permissions:\n  1: deny\n  bash:\n    "git push *": deny\n', PermissionMode.AUTO)
    assert _bash(st, "git push origin main") == "deny"


@pytest.mark.parametrize(
    "body",
    [
        'permissions:\n  bash:\n    "git push *": deny\n  edit: [\n',  # YAML syntax error
        "permissions: deny\n",  # the section is not a mapping
        "permissions:\n- bash\n",
        "- permissions\n",  # the file is not a mapping
    ],
)
def test_a_permissions_file_that_cannot_be_read_denies_what_no_rule_allows(tmp_path, body):
    st = _state(tmp_path, body, PermissionMode.AUTO)
    assert _bash(st, "git push origin main") == "deny"
    assert _bash(st, "ls") == "allow"  # the builtin read-only allowlist still applies


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 000 file, so it cannot be made unreadable")
def test_a_permissions_file_the_os_refuses_to_read_denies_what_no_rule_allows(tmp_path):
    path = _config(tmp_path, 'permissions:\n  bash:\n    "git push *": deny\n')
    path.chmod(0o000)
    try:
        assert path.is_file()
        parsed, _ = load_permissions_config(path)
        assert parsed == list(UNREADABLE)
        st = PermissionState(mode=PermissionMode.AUTO, cwd=tmp_path, user_config=path)
        st.reload()
        assert _bash(st, "git push origin main") == "deny"
        assert _bash(st, "ls") == "allow"  # the builtin read-only allowlist still applies
    finally:
        path.chmod(0o600)


def test_a_permissions_file_that_is_not_utf8_denies_what_no_rule_allows(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes(b'permissions:\n  bash:\n    "git push *": deny\n# \xff\xfe\n')
    parsed, _ = load_permissions_config(path)
    assert parsed == list(UNREADABLE)
    st = PermissionState(mode=PermissionMode.AUTO, cwd=tmp_path, user_config=path)
    st.reload()
    assert _bash(st, "git push origin main") == "deny"
    assert _bash(st, "ls") == "allow"


def test_an_accepted_recipe_rules_file_with_an_invalid_action_fails_closed(tmp_path):
    from k3code.learning.projectstate import rules_path

    path = rules_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('permissions:\n  bash:\n    "git push *": Deny\n', encoding="utf-8")
    st = PermissionState(mode=PermissionMode.AUTO, cwd=tmp_path, user_config=tmp_path / "none.yaml")
    st.reload()
    assert _bash(st, "git push origin main") == "deny"


def test_a_trusted_project_config_with_an_invalid_action_keeps_its_deny_rules():
    parsed, hard = permissions_from_text(
        'permissions:\n  bash:\n    "git push *": deny\n    "make *": Allow\n  hardline: ["^shred "]\n'
    )
    assert rules.evaluate("bash", "git push origin main", parsed).action == "deny"
    assert rules.evaluate("bash", "make deploy", parsed).action == "deny"
    assert hard == ["^shred "]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "# only a comment\n",
        "model: x\n",
        "permissions:\n",
        "permissions: {}\n",
        'permissions:\n  bash: ask\n  edit:\n    "~/proj/**": allow\n  hardline: ["^shred "]\n',
    ],
)
def test_a_valid_or_empty_config_is_read_as_before_and_logs_nothing(tmp_path, body, caplog):
    path = _config(tmp_path, body)
    with caplog.at_level(logging.WARNING, logger="k3code.permissions.state"):
        parsed, _ = load_permissions_config(path)
    assert all(r.tool != "*" for r in parsed)
    assert not caplog.records
    assert load_permissions_config(tmp_path / "missing.yaml") == ([], [])


def test_valid_entries_parse_exactly_as_the_strict_parser_does():
    section = {"bash": "ask", "edit": {"~/proj/**": "allow", "/etc/**": "deny"}, "mcp__x__*": {"*": "allow"}}
    problems: list[str] = []
    assert rules.from_config(section, problems) == rules.from_config(section)
    assert problems == []


def test_the_strict_parser_still_raises_for_config_validation():
    with pytest.raises(ValueError):
        rules.from_config({"bash": {"git push *": "Deny"}})
    with pytest.raises(ValueError):
        rules.from_config({"bash": ["git push *"]})


def test_the_problem_is_logged_once_with_the_file_and_key_but_not_the_pattern(tmp_path, caplog):
    secret = "Bearer " + "x" * 12 + "fake"
    body = f'permissions:\n  bash:\n    "git push *": deny\n    "curl -H {secret} *": Deny\n'
    st = PermissionState(mode=PermissionMode.AUTO, cwd=tmp_path, user_config=_config(tmp_path, body))
    with caplog.at_level(logging.WARNING, logger="k3code.permissions.state"):
        st.reload()
        st.reload()
    (record,) = caplog.records
    msg = record.getMessage()
    assert str(tmp_path / "config.yaml") in msg and "permissions.bash" in msg and "entry 2" in msg
    assert "deny" in msg and secret not in msg


def test_doctor_names_the_entry_that_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text('permissions:\n  bash:\n    "git push *": deny\n    "rm *": Deny\n')
    check = doctor.check_permissions(home, tmp_path)
    assert check.status == doctor.WARN
    assert str(home / "config.yaml") in check.detail and "permissions.bash entry 2" in check.detail
    (home / "config.yaml").write_text('permissions:\n  bash:\n    "git push *": deny\n')
    assert doctor.check_permissions(home, tmp_path).status == doctor.OK
