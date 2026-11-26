"""The shell splits unquoted ``$IFS`` and expands unquoted braces after k3code reads the words: such a command is
judged like the command the shell finally runs (``rm${IFS}-rf${IFS}/`` and ``{rm,-rf,/}`` are ``rm -rf /``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code.permissions import decide

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
    ("ls {src,tests}", {"default": "allow", "auto": "allow", "yolo": "allow"}),
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
