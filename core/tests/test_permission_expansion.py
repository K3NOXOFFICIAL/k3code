"""The shell splits unquoted ``$IFS`` and expands unquoted braces after k3code reads the words: such a command is
judged like the command the shell finally runs (``rm${IFS}-rf${IFS}/`` and ``{rm,-rf,/}`` are ``rm -rf /``)."""

from __future__ import annotations

import time
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


# a brace expansion too large to read word by word was an ask (yolo allowed it): now denied in every mode, since
# what it runs is unknown (`{rm,-rf,/} {1..5000}`)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("cmd", ["echo {1..5000}", "echo {1..100}{1..100}"])
def test_brace_expansion_too_large_to_check_is_denied(cmd: str, mode: str, tmp_path: Path) -> None:
    got = _decide(cmd, mode, tmp_path)
    assert (got.action, got.hardline) == ("deny", "expansion-over-cap"), cmd


def test_brace_expansion_over_cap_is_denied_quickly(tmp_path: Path) -> None:
    start = time.perf_counter()
    assert _decide("echo {1..5000}", "yolo", tmp_path).hardline == "expansion-over-cap"
    assert time.perf_counter() - start < 1.0


def _decision_class(cmd: str, mode: str, cwd: Path) -> tuple:
    got = _decide(cmd, mode, cwd)
    return got.action, got.hardline, got.needs_human


# a brace expansion of up to 4096 words is checked word by word, so a common loop or list is judged like its short form
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("cmd", "short"),
    [
        ("for i in {1..100}; do echo $i; done", "for i in {1..3}; do echo $i; done"),
        ("printf '%s\\n' {1..1000}", "printf '%s\\n' 1 2 3"),
        ("echo {a..z}{a..z}", "echo aa ab ac"),
        ("echo x{,}{,}{,}{,}{,}{,}{,}", "echo x x x"),
        ("touch f{1..100}", "touch f1 f2 f3"),
    ],
)
def test_brace_expansion_under_cap_is_judged_like_its_short_form(
    cmd: str, short: str, mode: str, tmp_path: Path
) -> None:
    got = _decision_class(cmd, mode, tmp_path)
    assert got == _decision_class(short, mode, tmp_path), cmd
    assert got[0] != "deny", cmd


# under the cap these are denied for what they run (`rm -rf /`), not for their size
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("cmd", ["{rm,-rf,/} {1..100}", "rm${IFS}-rf${IFS}/ {1..100}"])
def test_expansion_under_cap_is_denied_by_its_content(cmd: str, mode: str, tmp_path: Path) -> None:
    got = _decide(cmd, mode, tmp_path)
    assert got.action == "deny" and got.hardline == _decide("rm -rf /", mode, tmp_path).hardline, cmd


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


_SUBST_RULES = {"cat *": "allow", "echo *": "allow", "git commit *": "allow"}


def _decide_rules(cmd: str, mode: str, cwd: Path):
    rules = from_config({"bash": _SUBST_RULES})
    return decide(mode=mode, tool="bash", args={"command": cmd}, cwd=cwd, user_rules=rules)


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize(
    "cmd",
    [
        "cat $(echo /etc/passwd)",
        "cat `echo /etc/passwd`",
        'cat "$(echo /etc/passwd)"',
        'cat "`echo /etc/passwd`"',
        "git commit -F $(echo /etc/shadow)",
        # before this change `allow` (each nested command was allowed); now an ask, accepted
        'git commit -m "$(cat msg)"',
    ],
)
def test_allow_never_covers_command_substitution_output(cmd: str, mode: str, tmp_path: Path) -> None:
    # auto mode may still convert the ask (its classifier and sandbox decide), but no rule allows it outright
    got = _decide_rules(cmd, mode, tmp_path)
    assert got.action != "allow" or (mode == "auto" and got.auto_allowed), cmd


@pytest.mark.parametrize("mode", ["default", "auto"])
def test_allow_still_covers_a_plain_command(mode: str, tmp_path: Path) -> None:
    got = _decide_rules("echo hi", mode, tmp_path)
    assert (got.action, got.auto_allowed) == ("allow", False)


def test_command_substitution_in_single_quotes_is_a_literal() -> None:
    from k3code.permissions.hardline import command_substitution

    assert not command_substitution("echo 'a $(b)'")
    assert not command_substitution("echo 'a `b`'")
    assert not command_substitution("cat \\$(x)")
    assert not command_substitution("cat <(echo x)")
    assert command_substitution("echo \"'$(b)'\"")
    assert command_substitution("echo `b`")


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize(("cmd", "want"), KEEP)
def test_unexpanded_command_keeps_its_decision(cmd: str, want: dict, mode: str, tmp_path: Path) -> None:
    got = _decide(cmd, mode, tmp_path)
    assert (got.action, got.needs_human) == (want[mode], False), cmd


_RULE_LAYERS = {"none": None, "printf+echo": {"printf *": "allow", "echo *": "allow"}}


def _decide_layer(cmd: str, mode: str, cwd: Path, layer: str):
    rules = from_config({"bash": _RULE_LAYERS[layer]}) if _RULE_LAYERS[layer] else None
    return decide(mode=mode, tool="bash", args={"command": cmd}, cwd=cwd, user_rules=rules)


@pytest.mark.parametrize("layer", _RULE_LAYERS)
@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize(
    "cmd", ["printf -v x 'a[%s(id)]' '$'; echo $[x]", "echo $[x]", "echo $[1+1]", 'echo "$[x]"', "echo $((x))"]
)
def test_allow_never_covers_old_arithmetic(cmd: str, mode: str, layer: str, tmp_path: Path) -> None:
    # `$[x]` expands x and runs a `$(...)` in its value; yolo, which allows any non-hardline command, is not covered
    got = _decide_layer(cmd, mode, tmp_path, layer)
    assert got.action != "allow" or (mode == "auto" and got.auto_allowed), cmd


def test_ansi_c_quote_is_no_expansion(tmp_path: Path) -> None:
    assert _decide_layer("echo $'a\\tb'", "default", tmp_path, "printf+echo").action == "allow"


@pytest.mark.parametrize("layer", _RULE_LAYERS)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("cmd", ["{rm,-rf,/} {1..100}", "rm${IFS}-rf${IFS}/ {1..100}"])
def test_over_cap_expansion_is_never_allowed(cmd: str, mode: str, layer: str, tmp_path: Path) -> None:
    got = _decide_layer(cmd, mode, tmp_path, layer)
    assert got.action != "allow", cmd
    if mode == "yolo":
        assert got.action == "deny" and got.hardline, cmd


@pytest.mark.parametrize("layer", _RULE_LAYERS)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("cmd", ["{bash,-c,'{rm,-rf,/}'}", "bash${IFS}-c${IFS}'rm${IFS}-rf${IFS}/'"])
def test_command_made_by_expansion_is_expanded_again(cmd: str, mode: str, layer: str, tmp_path: Path) -> None:
    got = _decide_layer(cmd, mode, tmp_path, layer)
    assert got.action == "deny" and got.hardline, cmd


@pytest.mark.parametrize("layer", _RULE_LAYERS)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("cmd", ["bash -c 'echo hi'", "bash -c 'ls {src,tests}'", 'sh -c "echo ${HOME}"'])
def test_nested_shell_keeps_its_decision(cmd: str, mode: str, layer: str, tmp_path: Path) -> None:
    got = _decide_layer(cmd, mode, tmp_path, layer)
    want = {"default": "ask", "auto": "allow", "yolo": "allow"}[mode]
    assert (got.action, got.hardline, got.needs_human) == (want, None, False), cmd


# ── a cd before or in a nested command moves the relative targets of its expanded form ──

CD_THEN_EXPANDED_DELETE = [
    "cd / && bash -c 'rm${IFS}-rf${IFS}*'",
    "cd / && rm${IFS}-rf${IFS}*",
    "bash -c 'cd / && rm${IFS}-rf${IFS}*'",
    "cd / && sh -c '{rm,-rf,*}'",
    "cd $X && bash -c 'rm${IFS}-rf${IFS}*'",  # a cd the check cannot resolve: assume the worst
    "pushd / && bash -c 'rm${IFS}-rf${IFS}*'",
]


@pytest.mark.parametrize("mode", ["default", "accept-edits", "auto", "yolo"])
@pytest.mark.parametrize("cmd", CD_THEN_EXPANDED_DELETE)
def test_a_cd_counts_for_the_expanded_form_of_a_nested_command(tmp_path: Path, cmd: str, mode: str) -> None:
    # as strict as `rm -rf /*`: a hardline, so not even yolo runs it
    dec = decide(mode=mode, tool="bash", args={"command": cmd}, cwd=tmp_path)
    assert dec.action == "deny" and dec.hardline, (cmd, mode, dec.action, dec.message)


@pytest.mark.parametrize("cmd", ["cd src && ls", "cd src && bash -c 'ls${IFS}-la'"])
def test_a_harmless_cd_before_a_nested_command_keeps_its_hardline_verdict(tmp_path: Path, cmd: str) -> None:
    (tmp_path / "src").mkdir()
    for mode in ("default", "auto", "yolo"):
        dec = decide(mode=mode, tool="bash", args={"command": cmd}, cwd=tmp_path)
        assert dec.action != "deny" and dec.hardline is None, (cmd, mode, dec.message)
