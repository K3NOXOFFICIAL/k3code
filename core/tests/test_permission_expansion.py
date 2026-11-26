"""The shell splits unquoted ``$IFS`` and expands unquoted braces after k3code reads the words: such a command is
judged like the command the shell finally runs (``rm${IFS}-rf${IFS}/`` and ``{rm,-rf,/}`` are ``rm -rf /``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code.permissions import decide
from k3code.permissions.rules import from_config

MODES = ("default", "auto", "yolo")

# (the spelling the shell re-splits, the plain form it runs)
RESPLIT = [
    ("rm${IFS}-rf${IFS}/", "rm -rf /"),
    ("rm$IFS-rf$IFS/", "rm -rf /"),
    ("rm -rf${IFS}/", "rm -rf /"),
    ("rm -rf ${IFS%?}/", "rm -rf /"),
    ("rm${IFS:0:1}-rf${IFS:0:1}/", "rm -rf /"),
    ("{rm,-rf,/}", "rm -rf /"),
    ("{rm,-rf${IFS}/}", "rm -rf /"),
    ("rm -rf /{,}", "rm -rf /"),
    ("true # it's\nrm${IFS}-rf${IFS}/", "rm -rf /"),
    ("git${IFS}push${IFS}--force", "git push --force"),
    ("git push${IFS}--force", "git push --force"),
    ("sudo${IFS}rm${IFS}-rf${IFS}/", "sudo rm -rf /"),
    ("bash -c 'rm${IFS}-rf${IFS}/'", "rm -rf /"),
    ("echo $(rm${IFS}-rf${IFS}/)", "rm -rf /"),
    ("mount${IFS}-t${IFS}ext4${IFS}/dev/sdb1${IFS}/mnt", "mount -t ext4 /dev/sdb1 /mnt"),
    ("cat x${IFS}.env", "cat x .env"),
    ("cat {x,.env}", "cat x .env"),
    ("cat .{env,x}", "cat .env .x"),
]

# decided on the commit before this change, hardcoded: (command, {mode: action}), none needs a human
UNCHANGED = [
    ('echo "${IFS}"', {"default": "ask", "auto": "allow", "yolo": "allow"}),
    ('printf %s "$IFS"', {"default": "ask", "auto": "allow", "yolo": "allow"}),
    ("awk '{print $1}' f", {"default": "ask", "auto": "allow", "yolo": "allow"}),
    ("find . -name '*.{c,h}'", {"default": "allow", "auto": "allow", "yolo": "allow"}),
    ("git log --format='%h {x,y}'", {"default": "allow", "auto": "allow", "yolo": "allow"}),
    ("ls ${HOME}", {"default": "ask", "auto": "allow", "yolo": "allow"}),
    # a brace the shell expands is a word no rule saw (see EXPANDED_ARG): it asks, auto mode converts that ask
    ("ls {src,tests}", {"default": "ask", "auto": "allow", "yolo": "allow"}),
]

# an argument the shell expands after the rule matched: the builtin read-only allows and `cat *` must not cover it
EXPANDED_ARG = [
    ("cat x${IFS}/etc/passwd", None),
    ("ls x${IFS}/etc", None),
    ("head -n 1${IFS}/etc/passwd", None),
    ("grep -e x${IFS}/etc/passwd src", None),
    ("cat {x,/etc/passwd}", None),
    ("df -t x${IFS}/etc", None),
    ("ls {src,/etc}", None),
    ("x=' '; cat a${x}/etc/passwd", None),
    ("cat a${x:- /etc/passwd}", "cat *"),
    ("cat $X", "cat *"),
    ("cat ${X}", "cat *"),
    ("cat a$x", "cat *"),
    ('cat "$X"', "cat *"),
    ('cat "${X}"', "cat *"),
]

# no `$` and no brace the shell expands: decided as before (command, {mode: action})
KEEP = [
    ("echo hi", {"default": "ask", "auto": "allow"}),
    ("ls src", {"default": "allow", "auto": "allow"}),
    ("cat README.md", {"default": "allow", "auto": "allow"}),
    ("grep -n foo src", {"default": "allow", "auto": "allow"}),
    ("awk '{print $1}' f", {"default": "ask", "auto": "allow"}),
    ("find . -name '*.{c,h}'", {"default": "allow", "auto": "allow"}),
]


def _decide(cmd: str, mode: str, cwd: Path):
    return decide(mode=mode, tool="bash", args={"command": cmd}, cwd=cwd)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("cmd", "plain"), RESPLIT)
def test_resplit_form_is_judged_like_its_plain_form(cmd: str, plain: str, mode: str, tmp_path: Path) -> None:
    want, got = _decide(plain, mode, tmp_path), _decide(cmd, mode, tmp_path)
    assert (got.action, got.needs_human) == (want.action, want.needs_human), cmd
    if mode != "yolo" and want.action != "allow":
        assert got.action != "allow", cmd


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "cmd", ["rm${IFS}-rf${IFS}/", "{rm,-rf,/}", "bash -c 'rm${IFS}-rf${IFS}/'", "cat .{env,x}", "cat x${IFS}.env"]
)
def test_resplit_root_delete_is_a_hardline(cmd: str, mode: str, tmp_path: Path) -> None:
    got = _decide(cmd, mode, tmp_path)
    assert got.action == "deny" and got.hardline, cmd


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("cmd", "want"), UNCHANGED)
def test_quoted_or_unexpanded_forms_keep_their_decision(cmd: str, want: dict, mode: str, tmp_path: Path) -> None:
    got = _decide(cmd, mode, tmp_path)
    assert (got.action, got.needs_human) == (want[mode], False), cmd


def test_brace_expansion_keeps_prefix_and_suffix() -> None:
    from k3code.permissions.hardline import _expanded_text

    assert _expanded_text("make ./build/{a,b}.o") == "make ./build/a.o ./build/b.o"
    assert _expanded_text("touch f{1..3}") == "touch f1 f2 f3"
    assert _expanded_text("git rev-parse @{u}") is None
    assert _expanded_text("find . -exec rm {} \\;") is None


@pytest.mark.parametrize("mode", ["default", "auto"])
def test_brace_expansion_too_large_to_check_needs_a_human(mode: str, tmp_path: Path) -> None:
    got = _decide("echo {a,b}{a,b}{a,b}{a,b}{a,b}{a,b}{a,b}", mode, tmp_path)
    assert (got.action, got.needs_human) == ("ask", True)


def _decide_with(cmd: str, mode: str, cwd: Path, allow: str | None):
    rules = from_config({"bash": {allow: "allow"}}) if allow else None
    return decide(mode=mode, tool="bash", args={"command": cmd}, cwd=cwd, user_rules=rules)


@pytest.mark.parametrize(("cmd", "allow"), EXPANDED_ARG)
def test_allow_never_covers_a_shell_expanded_argument(cmd: str, allow: str | None, tmp_path: Path) -> None:
    assert _decide_with(cmd, "default", tmp_path, allow).action != "allow", cmd
    # auto mode may still convert the ask (its classifier and sandbox decide), but no rule allows it outright
    got = _decide_with(cmd, "auto", tmp_path, allow)
    assert got.action != "allow" or got.auto_allowed, cmd


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize("cmd", ["$X /etc/passwd", "${CMD} x", '"$X" x', "{cat,/etc/passwd}"])
@pytest.mark.parametrize("allow", [None, "*"])
def test_expanded_command_word_needs_a_human(cmd: str, allow: str | None, mode: str, tmp_path: Path) -> None:
    got = _decide_with(cmd, mode, tmp_path, allow)
    assert (got.action, got.needs_human) == ("ask", True), cmd


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize(("cmd", "want"), KEEP)
def test_unexpanded_command_keeps_its_decision(cmd: str, want: dict, mode: str, tmp_path: Path) -> None:
    got = _decide(cmd, mode, tmp_path)
    assert (got.action, got.needs_human) == (want[mode], False), cmd
