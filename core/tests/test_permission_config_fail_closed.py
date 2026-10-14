"""A malformed ``permissions:`` entry fails closed: it never drops the file's other rules (deny rules included)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from k3code import doctor
from k3code.config import Settings
from k3code.permissions import PermissionMode, hardline, rules
from k3code.permissions.state import (
    UNREADABLE,
    UNREADABLE_HARDLINE,
    PermissionState,
    load_permissions_config,
    parse_permissions,
    permissions_from_text,
)

#: YAML spellings of an action that is not exactly allow|ask|deny (``no`` is a bool, an empty value is null).
BAD_ACTIONS = ["Deny", '"deny "', "block", "no", "", "[deny]", "{a: deny}", "1"]
#: Tool entries that are neither an action nor a pattern→action mapping.
BAD_TOOL_ENTRIES = ['["git push *"]', "3", "true"]
#: ``permissions.hardline`` values that are not a list of regexes: a scalar (a string was split into one regex per
#: character, and ``^`` matched every command), a mapping, an item that is not text, an item ``re`` cannot compile.
BAD_HARDLINES = [
    pytest.param("5", id="int"),
    pytest.param("true", id="true"),
    pytest.param("false", id="false"),
    pytest.param("'^shred '", id="string"),
    pytest.param("{a: b}", id="mapping"),
    pytest.param("[5]", id="int-item"),
    pytest.param('["^shred ", null]', id="null-item"),
    pytest.param('["^shred ", [x]]', id="list-item"),
    pytest.param('["("]', id="re-error"),
    pytest.param('["a{4294967296}"]', id="overflow"),
    pytest.param('["' + "(" * 1000 + ")" * 1000 + '"]', id="recursion"),
]


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
    assert st.decide("bash", {"command": "ls"}).hardline  # the read-only allowlist too: its hardline is lost


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
        assert st.decide("bash", {"command": "ls"}).hardline  # the read-only allowlist too: its hardline is lost
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
    assert st.decide("bash", {"command": "ls"}).hardline  # the read-only allowlist too: its hardline is lost


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
        "permissions:\n  hardline:\n",
        "permissions:\n  hardline: []\n",
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


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('permissions:\n  bash:\n    "git push *": deny\nwhen: 2026-13-45\n', id="date"),
        pytest.param("permissions:\n  bash: !!int deny\n", id="int-tag"),
        pytest.param("x: " + "[" * 1000 + "]" * 1000 + "\n", id="deep"),
    ],
)
def test_yaml_the_parser_cannot_build_denies_what_no_rule_allows_without_raising(tmp_path, body):
    """safe_load raises ValueError and RecursionError besides YAMLError; they aborted every gateway turn."""
    assert parse_permissions(body)[0] == list(UNREADABLE)
    st = _state(tmp_path, body, PermissionMode.AUTO)
    assert _bash(st, "git push origin main") == "deny"
    assert st.decide("bash", {"command": "ls"}).hardline  # the read-only allowlist too: its hardline is lost


#: Config text whose ``permissions:`` cannot be read at all, each holding a hardline the user wrote: it is lost with it.
UNPARSEABLE_WITH_HARDLINE = [
    pytest.param("permissions:\n  hardline: ['^ssh ']\nx: 2026-13-45\n", id="date"),
    pytest.param("permissions:\n  hardline: ['^ssh ']\n  bash: !!int deny\n", id="int-tag"),
    pytest.param("permissions:\n  hardline: ['^ssh ']\nx: " + "[" * 1000 + "]" * 1000 + "\n", id="deep"),
    pytest.param("permissions:\n  hardline: ['^ssh ']\n  edit: [\n", id="syntax-error"),
    pytest.param("- permissions:\n    hardline: ['^ssh ']\n", id="file-not-a-mapping"),
    pytest.param("permissions:\n- hardline: ['^ssh ']\n", id="section-not-a-mapping"),
]


@pytest.mark.parametrize("body", UNPARSEABLE_WITH_HARDLINE)
def test_a_config_that_cannot_be_parsed_hardline_denies_bash_in_yolo(tmp_path, body):
    """Yolo skips the rules for bash, so the UNREADABLE deny rule alone let the user's own hardline lapse."""
    assert parse_permissions(body)[:2] == (list(UNREADABLE), [UNREADABLE_HARDLINE])
    st = _state(tmp_path, body, PermissionMode.YOLO)
    decision = st.decide("bash", {"command": "ssh host systemctl stop x"})
    assert decision.action == "deny" and decision.hardline


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 000 file, so it cannot be made unreadable")
def test_a_config_the_os_refuses_to_read_hardline_denies_bash_in_yolo(tmp_path):
    path = _config(tmp_path, "permissions:\n  hardline: ['^ssh ']\n")
    path.chmod(0o000)
    try:
        assert load_permissions_config(path) == (list(UNREADABLE), [UNREADABLE_HARDLINE])
        st = PermissionState(mode=PermissionMode.YOLO, cwd=tmp_path, user_config=path)
        st.reload()
        assert st.decide("bash", {"command": "ssh host systemctl stop x"}).hardline
    finally:
        path.chmod(0o600)


def test_a_config_that_is_not_utf8_hardline_denies_bash_in_yolo(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes(b"permissions:\n  hardline: ['^ssh ']\n# \xff\xfe\n")
    assert load_permissions_config(path) == (list(UNREADABLE), [UNREADABLE_HARDLINE])
    st = PermissionState(mode=PermissionMode.YOLO, cwd=tmp_path, user_config=path)
    st.reload()
    assert st.decide("bash", {"command": "ssh host systemctl stop x"}).hardline


@pytest.mark.parametrize("body", UNPARSEABLE_WITH_HARDLINE)
def test_doctor_fails_the_permissions_check_on_a_config_that_cannot_be_parsed(tmp_path, body):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(body, encoding="utf-8")
    check = doctor.check_permissions(home, tmp_path)
    assert check.status == doctor.FAIL and str(home / "config.yaml") in check.detail


@pytest.mark.parametrize("bad", BAD_HARDLINES)
def test_a_hardline_value_that_is_not_a_list_of_regexes_fails_closed_without_raising(bad):
    parsed, hard, problems = parse_permissions(f'permissions:\n  bash:\n    "git push *": deny\n  hardline: {bad}\n')
    assert hard == [UNREADABLE_HARDLINE]
    assert problems and all(p.startswith("permissions.hardline") for p in problems)
    assert rules.evaluate("bash", "git push origin main", parsed).action == "deny"  # the file's rules are kept


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("bad", BAD_HARDLINES)
def test_a_bad_hardline_value_hardline_denies_every_bash_command_in_every_mode(tmp_path, bad, mode):
    """A deny rule would not do: yolo skips the rules for bash, the hardline runs in every mode."""
    st = _state(tmp_path, f"permissions:\n  hardline: {bad}\n", mode)
    decision = st.decide("bash", {"command": "ls"})
    assert decision.action == "deny" and decision.hardline


def test_a_bad_hardline_value_keeps_the_builtin_hardline_and_leaves_other_tools_alone(tmp_path):
    st = _state(tmp_path, "permissions:\n  hardline: 5\n", PermissionMode.YOLO)
    assert st.decide("bash", {"command": "mkfs.ext4 /dev/sda1"}).hardline == "mkfs"  # the builtin list runs first
    assert hardline.check("ls", st.hardline_extra)  # `!cmd` in the composer checks the same list
    assert st.decide("read", {"path": str(tmp_path / "x")}).action == "allow"  # only bash has a hardline


def test_a_hardline_value_that_breaks_mid_session_denies_bash_on_the_next_reload(tmp_path):
    path = _config(tmp_path, 'permissions:\n  hardline: ["^shred "]\n')
    st = PermissionState(mode=PermissionMode.YOLO, cwd=tmp_path, user_config=path)
    st.reload()
    assert _bash(st, "ls") == "allow"
    path.write_text("permissions:\n  hardline: 5\n", encoding="utf-8")
    st.reload()  # the gateway reloads before every turn (server.py _run_one_turn); this raised TypeError
    assert _bash(st, "ls") == "deny"
    path.write_text('permissions:\n  hardline: ["^shred "]\n', encoding="utf-8")
    st.reload()
    assert _bash(st, "ls") == "allow"


def test_a_bad_hardline_value_in_the_accepted_rules_file_denies_bash(tmp_path):
    from k3code.learning.projectstate import rules_path

    path = rules_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("permissions:\n  hardline: 5\n", encoding="utf-8")
    st = PermissionState(mode=PermissionMode.YOLO, cwd=tmp_path, user_config=tmp_path / "none.yaml")
    st.reload()
    assert _bash(st, "ls") == "deny"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", []), ("[]", []), (r"['^shred ', '\bmake\b']", ["^shred ", r"\bmake\b"])],
)
def test_a_valid_hardline_value_is_read_as_before(value, expected):
    assert parse_permissions(f"permissions:\n  hardline: {value}\n")[1:] == (expected, [])


def test_a_bad_hardline_problem_names_the_file_and_key_but_not_the_value(tmp_path, caplog):
    secret = "Bearer " + "x" * 12 + "fake"
    path = _config(tmp_path, f"permissions:\n  hardline: ['^shred ', 5, '{secret}(']\n")
    with caplog.at_level(logging.WARNING, logger="k3code.permissions.state"):
        load_permissions_config(path)
        load_permissions_config(path)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2  # entries 2 and 3, once each
    assert all(str(path) in m and "every bash command" in m and secret not in m for m in messages)
    assert "permissions.hardline entry 2" in messages[0] and "permissions.hardline entry 3" in messages[1]


@pytest.mark.parametrize("bad", BAD_HARDLINES)
def test_doctor_fails_the_permissions_check_on_a_bad_hardline_value(tmp_path, bad):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(f"permissions:\n  hardline: {bad}\n", encoding="utf-8")
    check = doctor.check_permissions(home, tmp_path)
    assert check.status == doctor.FAIL
    assert str(home / "config.yaml") in check.detail and "permissions.hardline" in check.detail


async def test_doctor_runs_every_check_with_a_bad_hardline_value(tmp_path, monkeypatch):
    """`k3code doctor` stopped with a TypeError traceback at this check, so it could not say what was wrong."""
    (tmp_path / "config.yaml").write_text("permissions:\n  hardline: 5\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "default_project_dir", lambda: tmp_path)
    checks = await doctor.run_checks(Settings(), probe=False, home=tmp_path)
    assert checks[-1].name == "worktrees"  # the last check ran
    (permissions,) = [c for c in checks if c.name == "permissions"]
    assert permissions.status == doctor.FAIL and "permissions.hardline" in permissions.detail
