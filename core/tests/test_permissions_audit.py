"""Regression tests for the permission-engine security audit (nine findings).

Every command here is only ever handed to ``decide()`` / the parser as a STRING: nothing is executed. Each table
pins what a once-allowed spelling must now do; the controls pin the ordinary commands that must keep working.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from k3code.permissions import Rule, decide, hardline
from k3code.permissions.engine import sensitive_path, suggest_rules
from k3code.permissions.hardline import normalize_argv, parse

MODES = ("default", "auto", "yolo")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A FAKE home next to the project (the real one is never touched)."""
    h = tmp_path / "home" / "alice"
    h.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return h


@pytest.fixture
def proj(tmp_path: Path, home: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _bash(cmd: str, mode: str, cwd: Path, *, headless: bool = False, rules: list[Rule] | None = None):
    return decide(mode=mode, tool="bash", args={"command": cmd}, cwd=cwd, headless=headless, user_rules=rules)


def _assert_hardline(cmd: str, cwd: Path) -> None:
    for mode in MODES:
        d = _bash(cmd, mode, cwd)
        assert d.action == "deny" and d.hardline, (cmd, mode, d.action, d.message)


def _assert_needs_human(cmd: str, cwd: Path) -> None:
    for mode in ("default", "auto"):
        d = _bash(cmd, mode, cwd)
        assert (d.action, d.needs_human, d.hardline) == ("ask", True, None), (cmd, mode, d.action, d.message)
    assert _bash(cmd, "auto", cwd, headless=True).action == "deny", cmd


# ── 1. `<>` and `>|` are writes ──

CLOBBER_AND_READWRITE = [
    "cat payload 1<>~/.bashrc",
    "git log 1<>~/.profile",
    "ls 1<>.git/config",
    "cat x <>~/.bashrc",
    "cat x >|~/.bashrc",
    "cat x >| ~/.bashrc",
    "ls 2>|/etc/cron.d/x",
    "cat x >|/etc/cron.d/x",
]


@pytest.mark.parametrize("cmd", CLOBBER_AND_READWRITE)
def test_readwrite_and_clobber_redirects_void_the_read_only_allowlist(proj: Path, cmd: str) -> None:
    assert _bash(cmd, "default", proj).action == "ask", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "cat payload 1<>~/.bashrc",
        "git log 1<>~/.profile",
        "cat x >|~/.bashrc",
        "cat x >| /etc/cron.d/x",
        "ls 2>|/etc/cron.d/x",
    ],
)
def test_auto_mode_denies_them_outside_the_project_like_a_plain_redirect(proj: Path, cmd: str) -> None:
    assert _bash(cmd, "auto", proj).action == "deny", cmd
    assert _bash(cmd.replace("<>", ">").replace(">|", ">"), "auto", proj).action == "deny", cmd


def test_clobber_redirect_stays_one_command_and_ordinary_redirects_keep_their_decisions(proj: Path) -> None:
    assert parse("cat x >|out.txt").subs == ["cat x >|out.txt"]
    assert parse("a | b || c >| d").subs == ["a", "b", "c >| d"]
    for cmd in ("ls 2>/dev/null", "git status 2>&1", "cat README.md", "ls -la"):
        assert _bash(cmd, "default", proj).action == "allow", cmd


def test_input_redirect_of_the_readwrite_form_is_a_credential_read(proj: Path, home: Path) -> None:
    (home / ".ssh").mkdir()
    for cmd in ("tr a b <>.env", "tr a b 0<>~/.ssh/id_rsa"):
        d = _bash(cmd, "auto", proj)
        assert (d.action, d.hardline) == ("deny", "sensitive-path"), cmd


# ── 2. shell keywords and braces ──

KEYWORD_HARDLINE = [
    "if true; then rm -rf /; fi",
    "for i in 1; do rm -rf /; done",
    "{ rm -rf /; }",
    "! rm -rf /",
    "if true; then true; else rm -rf /; fi",
    "if false; then true; elif true; then rm -rf /; fi",
    "f(){ rm -rf /; }; f",
    "function f { rm -rf /; }; f",
    "while true; do rm -rf ~; done",
    "until false; do rm -rf /; done",
    "select x in a; do rm -rf /; done",
    "case x in x) rm -rf / ;; esac",
    "coproc rm -rf /",
    "coproc job { rm -rf /; }",
    "if true; then env; fi",
    "if true; then bash -c 'rm -rf /'; fi",
    "if true; then eval 'rm -rf /'; fi",
    "if rm -rf /; then true; fi",
    "time rm -rf /",
    "if true; then sudo rm -rf /; fi",
]


@pytest.mark.parametrize("cmd", KEYWORD_HARDLINE)
def test_shell_keywords_and_braces_do_not_hide_a_hardline_command(proj: Path, cmd: str) -> None:
    _assert_hardline(cmd, proj)


USER_DENY_PUSH = [Rule(tool="bash", pattern="git push *", action="deny")]
KEYWORD_RULE_DENY = [
    "if true; then git push o x; fi",
    "{ git push; }",
    "for i in 1; do git push; done",
    "while true; do git push origin x; done",
    "! git push",
    "f(){ git push; }; f",
    "if true; then true; else git push; fi",
]


@pytest.mark.parametrize("cmd", KEYWORD_RULE_DENY)
def test_shell_keywords_and_braces_do_not_hide_a_user_deny_rule(proj: Path, cmd: str) -> None:
    for mode in ("default", "auto"):
        assert _bash(cmd, mode, proj, rules=USER_DENY_PUSH).action == "deny", (cmd, mode)


NORMALIZE_KEYWORDS = [
    (["then", "rm", "-rf", "x"], ["rm", "-rf", "x"]),
    (["do", "git", "push"], ["git", "push"]),
    (["{", "git", "push"], ["git", "push"]),
    (["!", "git", "push"], ["git", "push"]),
    (["else", "if", "true"], ["true"]),
    (["function", "f", "{", "rm", "x"], ["rm", "x"]),
    (["coproc", "job", "{", "rm", "x"], ["rm", "x"]),
    (["coproc", "rm", "x"], ["rm", "x"]),
    (["done"], []),
    (["}"], []),
    (["for", "i", "in", "a", "b"], []),
    (["case", "x", "in"], []),
    (["echo", "then"], ["echo", "then"]),
    (["git", "log", "--", "done"], ["git", "log", "--", "done"]),
]


@pytest.mark.parametrize(("argv", "expected"), NORMALIZE_KEYWORDS)
def test_normalize_argv_drops_reserved_words(argv: list[str], expected: list[str]) -> None:
    assert normalize_argv(argv) == expected


def test_an_always_rule_is_never_offered_for_a_shell_keyword(proj: Path) -> None:
    d = _bash("if true; then git commit -m x; fi", "default", proj)
    patterns = [r.pattern for r in suggest_rules("bash", d)]
    assert "git commit *" in patterns
    assert not any(p.split()[0] in ("then", "if", "fi", "do", "done", "{", "}") for p in patterns), patterns


def test_ordinary_control_flow_keeps_its_decisions(proj: Path) -> None:
    for cmd in ("for i in 1 2; do echo $i; done", "if true; then echo hi; fi", "echo then done fi"):
        assert _bash(cmd, "default", proj).action == "ask", cmd
        assert _bash(cmd, "auto", proj).action == "allow", cmd


# ── 3. the parse-depth cutoff ──


def _nest_subst(levels: int, inner: str = "rm -rf /") -> str:
    cmd = inner
    for _ in range(levels):
        cmd = f"echo $({cmd})"
    return cmd


def _nest_bash_c(levels: int, inner: str = "rm -rf /") -> str:
    """`bash -c "bash -c \"...\""`: double quotes with only the needed escapes, so the text grows linearly."""
    cmd = inner
    for _ in range(levels):
        escaped = cmd.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        cmd = f'bash -c "{escaped}"'
    return cmd


def test_the_audit_command_is_seven_levels_deep() -> None:
    assert _nest_subst(7) == "echo $(echo $(echo $(echo $(echo $(echo $(echo $(rm -rf /)))))))"
    assert _nest_bash_c(2) == 'bash -c "bash -c \\"rm -rf /\\""'


@pytest.mark.parametrize("make", [_nest_subst, _nest_bash_c])
def test_six_levels_are_still_denied(proj: Path, make) -> None:
    _assert_hardline(make(6), proj)


@pytest.mark.parametrize("levels", [7, 8, 12])
@pytest.mark.parametrize("make", [_nest_subst, _nest_bash_c])
def test_nesting_past_the_cutoff_is_never_allowed_without_a_human(proj: Path, make, levels: int) -> None:
    cmd = make(levels)
    _assert_needs_human(cmd, proj)
    d = _bash(cmd, "yolo", proj)  # yolo has no ask path: it is denied instead
    assert d.action == "deny" and d.hardline, cmd
    assert hardline.parse(cmd).unterminated


def test_harmless_deep_nesting_needs_a_human_too(proj: Path) -> None:
    _assert_needs_human(_nest_subst(9, "date"), proj)


def test_shallow_substitutions_are_unaffected(proj: Path) -> None:
    assert not hardline.parse(_nest_subst(4, "date")).unterminated
    assert _bash(_nest_subst(4, "date"), "auto", proj).action == "allow"


def test_absurd_nesting_does_not_crash_the_parser(proj: Path) -> None:
    cmd = "echo " + "$(echo " * 3000 + "x" + ")" * 3000
    for mode in ("default", "auto", "yolo"):
        d = _bash(cmd, mode, proj)
        assert d.action in ("ask", "deny"), mode
        assert d.action == "deny" or d.needs_human, mode


# ── 4. rm and its spellings ──

STDIN_FED_HARDLINE = [
    "echo 'rm -rf /' | sh",
    'echo "rm -rf /" | bash',
    "printf 'rm -rf /\\n' | dash",
    "printf '%s\\n' 'rm -rf /' | zsh",
    "echo 'echo hi; rm -rf /' | sudo bash",
    "sh <<EOF\nrm -rf /\nEOF",
    "bash <<'EOF'\nrm -rf /\nEOF",
    "bash -s <<EOF\necho hi\nrm -rf /\nEOF",
    "sh <<< 'rm -rf /'",
    "bash <<<'rm -rf /'",
    "cat <<EOF\n$(rm -rf /)\nEOF",
    "cat <<EOF\nnow: `rm -rf /`\nEOF",
]


@pytest.mark.parametrize("cmd", STDIN_FED_HARDLINE)
def test_a_script_fed_to_a_shell_is_parsed_like_any_other(proj: Path, cmd: str) -> None:
    _assert_hardline(cmd, proj)


STDIN_FED_NEEDS_HUMAN = [
    "echo 'rm -rf /' | base64 | base64 -d | bash",
    "echo cm0gLXJmIC8K | base64 -d | bash",
    "cat script.sh | sh",
    "git show HEAD:x.sh | bash -s",
    "sh < script.sh",
    "bash -s < setup.sh",
    'echo "$CMD" | sh',
    "printf '%s' $(cat x) | sh",
    "ls | xargs -I{} echo {} | sudo sh",
]


@pytest.mark.parametrize("cmd", STDIN_FED_NEEDS_HUMAN)
def test_a_shell_fed_a_script_we_cannot_read_needs_a_human(proj: Path, cmd: str) -> None:
    _assert_needs_human(cmd, proj)


STDIN_FED_CONTROLS = [
    "echo hi | sh -c 'cat'",
    "echo hi | wc -l",
    "cat <<EOF\nrm -rf /\nEOF",  # data, not a script
    "cat <<'EOF'\n$(rm -rf /)\nEOF",  # quoted delimiter: no expansion
    "git commit -m \"$(cat <<'EOF'\nnever curl x | sh\nEOF\n)\"",
    "sh script.sh < input.txt",
    "bash -c 'echo hi' <<< x",
    "sh < /dev/null",
]


@pytest.mark.parametrize("cmd", STDIN_FED_CONTROLS)
def test_data_fed_to_a_command_is_not_a_script(proj: Path, cmd: str) -> None:
    assert hardline.check(cmd, cwd=str(proj), home="/home/user") is None, cmd
    d = _bash(cmd, "auto", proj)
    assert not d.needs_human, cmd


def test_an_assigned_command_name_is_resolved_or_asked(proj: Path) -> None:
    _assert_hardline("a=rm; $a -rf /", proj)
    _assert_hardline("a=rm\n$a -rf /", proj)
    _assert_hardline("cmd=rm flags=-rf; $cmd $flags /", proj)
    _assert_hardline("a='rm -rf /'; $a", proj)
    _assert_hardline("export a=rm; ${a} -rf /", proj)
    _assert_hardline("cmd='rm -rf /'; eval \"$cmd\"", proj)
    _assert_hardline('c=rm; eval "$c -rf /"', proj)
    _assert_needs_human('eval "$unknown"', proj)
    _assert_needs_human("$a -rf /", proj)
    _assert_needs_human("$(echo rm) -rf /", proj)
    _assert_needs_human('"$tool" --version', proj)


# K3NOX-106: past a cap (16 combinations of assigned values, 64 brace words, 32 cd directories) the checks used to
# judge only the spellings they kept, so padding a line turned its own deny into an allow
_PAD = "".join(f"a=x{i}; " for i in range(16))
PAST_THE_CAPS = [
    _PAD + "a=rm; $a -rf /",  # the 17th value of $a was never put in
    "".join(f"f=a{i}; " for i in range(16)) + "f=.env; cat $f",
    "rm -rf {" + ",".join(f"d{i}" for i in range(64)) + ",/}",  # the 65th brace word was dropped
]


@pytest.mark.parametrize("cmd", PAST_THE_CAPS)
def test_a_line_past_an_expansion_cap_is_asked_never_allowed(proj: Path, cmd: str) -> None:
    _assert_needs_human(cmd, proj)
    d = _bash(cmd, "yolo", proj)
    assert d.action == "deny" and d.hardline, (d.action, d.message)  # yolo has nobody to ask


def test_a_line_that_cds_past_the_directory_cap_is_asked_never_allowed(proj: Path) -> None:
    cmd = "".join(f"cd {proj}/d{i}; " for i in range(32)) + "cd /; rm -rf *"  # the 33rd directory was dropped
    _assert_needs_human(cmd, proj)
    assert _bash(cmd, "yolo", proj).action == "deny"


def test_a_line_at_the_caps_is_still_resolved(proj: Path) -> None:
    _assert_hardline("".join(f"a=x{i}; " for i in range(15)) + "a=rm; $a -rf /", proj)  # 16 values: all checked
    _assert_hardline("rm -rf {" + ",".join(f"d{i}" for i in range(63)) + ",/}", proj)  # 64 words: all checked
    d = _bash("mkdir -p src/{a,b,c}/{x,y}", "auto", proj)
    assert not d.needs_human and d.action == "allow", d.message


RM_UNRESOLVED = [
    "rm -rf $(echo /)",
    "rm -rf `echo /`",
    "rm -rf $dir",
    'rm -rf "$dir"/*',
    "rm -rf ${BUILD_DIR}",
    "rm -r $(git rev-parse --show-toplevel)",
    "rm $f",
    "f=-rf; rm $f /tmp/x$g",
]


@pytest.mark.parametrize("cmd", RM_UNRESOLVED)
def test_an_rm_argument_that_is_an_unresolved_expansion_needs_a_human(proj: Path, cmd: str) -> None:
    _assert_needs_human(cmd, proj)


def test_rm_with_a_resolved_variable_is_decided_on_its_value(proj: Path, home: Path) -> None:
    _assert_hardline("d=/; rm -rf $d", proj)
    _assert_hardline("rm -rf $HOME", proj)
    _assert_hardline("rm -rf ${HOME}/", proj)
    for cmd in ("rm -rf $PWD/build", "rm -rf build/", "rm -rf node_modules", "rm -f out.log", "d=build; rm -rf $d"):
        assert hardline.check(cmd, cwd=str(proj), home=str(home)) is None, cmd
        d = _bash(cmd, "auto", proj)
        assert (d.action, d.needs_human) == ("allow", False), cmd
        assert _bash(cmd, "default", proj).action == "ask", cmd


WRAPPED_RM = [
    "busybox rm -rf /",
    "flock f rm -rf /",
    "flock -w 5 /tmp/lock rm -rf /",
    "flock -c 'rm -rf /' /tmp/lock",
    "flock --command 'rm -rf /' /tmp/lock",
    "strace rm -rf /",
    "strace -f -o /tmp/out -e trace=file rm -rf /",
    "watch rm -rf /",
    "watch -n 1 rm -rf /",
    "watch 'rm -rf /'",
    "unshare -r rm -rf /",
    "unshare --mount --propagation private rm -rf /",
    "chroot / rm -rf /",
    "chroot --userspec=u:g / rm -rf /",
    "setpriv --reuid 1000 --clear-groups rm -rf /",
    "fakeroot rm -rf /",
    "nsenter -t 1 -m rm -rf /",
    "find / -exec rm -rf {} +",
    "find / -name '*.log' -exec rm -rf {} \\;",
    "find ~ -execdir rm -rf {} \\;",
    "find . -exec bash -c 'rm -rf /' \\;",
    "sudo find /usr /etc -ok rm -rf {} \\;",
    "ssh localhost rm -rf /",
    "ssh -p 22 user@host 'cd /; rm -rf /'",
]


@pytest.mark.parametrize("cmd", WRAPPED_RM)
def test_wrappers_outside_the_old_list_do_not_hide_rm(proj: Path, cmd: str) -> None:
    _assert_hardline(cmd, proj)


@pytest.mark.parametrize(
    "cmd",
    [
        "busybox ls -la",
        "flock /tmp/lock make build",
        "strace -f ls",
        "watch -n 5 git status",
        "unshare -r id",
        "find . -name '*.pyc' -exec rm {} +",
        "find build -type f -exec rm -rf {} \\;",
        "find /tmp/work -delete",
        "chroot /mnt/img ls",
        "ssh host ls /",
    ],
)
def test_those_wrappers_around_harmless_commands_stay_clear_of_the_hardline(cmd: str) -> None:
    assert hardline.check(cmd, cwd="/work/proj", home="/home/user") is None, cmd


ROOT_AND_HOME_TARGETS = [
    "rm -rf /usr/*",
    "rm -rf /etc/*",
    "rm -rf /home/*",
    "rm -rf /var/*",
    "rm -rf /.*",
    "rm -rf /*/",
    "rm -rf /e*",
    "rm -rf ~/.*",
    "rm -rf ~/*",
    "rm -rf $HOME/.[!.]*",
    "rm -rf ${HOME}/.*",
    "rm -rf ~root",
    "rm -rf /usr/../etc/*",
    "rm -fr /usr/*/",
    "sudo rm -rf /usr/*",
]


@pytest.mark.parametrize("cmd", ROOT_AND_HOME_TARGETS)
def test_glob_targets_under_the_protected_roots_are_denied(proj: Path, cmd: str) -> None:
    _assert_hardline(cmd, proj)
    assert hardline.check(cmd, cwd=str(proj), home="/home/user") is not None, cmd


def test_a_glob_under_an_explicit_home_is_denied() -> None:
    assert hardline.check("rm -rf /home/user/.*", cwd="/work/proj", home="/home/user") == "rm-rf-home"
    assert hardline.check("rm -rf /home/user/*", cwd="/work/proj", home="/home/user") == "rm-rf-home"
    assert hardline.check("rm -rf /home/user/proj/*", cwd="/work/proj", home="/home/user") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /usr/local/share/old",
        "rm -rf /tmp/build/*",
        "rm -rf ./build/*",
        "rm -rf /etc/*.bak",
        "rm -rf /var/tmp/x*",
        "rm -rf ~/projects/old",
        "rm -rf ~/.cache/pip",
        "rm -rf ~/scratch/*",
        "rm -rf /home/user/proj/dist",
        "rm -rf build node_modules",
    ],
)
def test_narrower_targets_are_not_hardline(cmd: str) -> None:
    assert hardline.check(cmd, cwd="/home/user/proj", home="/home/user") is None, cmd


RELATIVE_TARGETS = [
    # (command, cwd, hardline name)
    ("rm -rf *", "/", "rm-rf-root"),
    ("rm -rf .", "/", "rm-rf-root"),
    ("rm -rf ./", "/", "rm-rf-root"),
    ("rm -rf etc", "/", "rm-rf-root"),
    ("rm -rf .*", "/", "rm-rf-root"),
    ("rm -rf *", "/home/user", "rm-rf-home"),
    ("rm -rf .*", "/home/user", "rm-rf-home"),
    ("rm -rf .", "/home/user", "rm-rf-home"),
    ("rm -rf ..", "/home/user/proj", "rm-rf-home"),
    ("rm -rf ../", "/home/user/proj", "rm-rf-home"),
    ("rm -rf ../..", "/home/user/proj", "rm-rf-home"),
    ("rm -rf ../*", "/home/user/proj", "rm-rf-home"),
    ("rm -rf *", "/usr", "rm-rf-root"),
    ("rm -rf $PWD/*", "/etc", "rm-rf-root"),
    ("cd / && rm -rf *", "/work/proj", "rm-rf-root"),
    ("cd /; rm -rf .", "/work/proj", "rm-rf-root"),
    ("cd ~ && rm -rf *", "/work/proj", "rm-rf-home"),
    ("cd && rm -rf .*", "/work/proj", "rm-rf-home"),
    ("cd .. && rm -rf *", "/home/user/proj", "rm-rf-home"),
    ("cd /usr && rm -rf *", "/work/proj", "rm-rf-root"),
    ("pushd / && rm -rf *", "/work/proj", "rm-rf-root"),
    ("bash -c 'cd / && rm -rf *'", "/work/proj", "rm-rf-root"),
    ("cd / ; (rm -rf *)", "/work/proj", "rm-rf-root"),
]


@pytest.mark.parametrize(("cmd", "cwd", "name"), RELATIVE_TARGETS)
def test_relative_rm_targets_resolve_against_the_working_directory(cmd: str, cwd: str, name: str) -> None:
    assert hardline.check(cmd, cwd=cwd, home="/home/user") == name, (cmd, cwd)


@pytest.mark.parametrize(
    ("cmd", "cwd"),
    [
        ("rm -rf *", "/work/proj"),
        ("rm -rf .", "/work/proj"),
        ("rm -rf ..", "/work/proj"),
        ("rm -rf *", "/home/user/proj"),
        ("rm -rf .*", "/home/user/proj"),
        ("rm -rf ../proj", "/home/user/proj"),
        ("rm -rf build", "/"),
        ("rm -rf tmp/*", "/"),
        ("cd build && rm -rf *", "/work/proj"),
        ("cd /tmp/x && rm -rf *", "/work/proj"),
        ("cd ~/proj && rm -rf *", "/work/proj"),
        ("cd src; rm -rf node_modules", "/work/proj"),
        ("rm -rf *", "/home/user/proj/build"),
    ],
)
def test_relative_targets_in_an_ordinary_directory_stay_allowed(cmd: str, cwd: str) -> None:
    assert hardline.check(cmd, cwd=cwd, home="/home/user") is None, (cmd, cwd)


def test_the_engine_passes_the_commands_directory_to_the_hardline(tmp_path: Path, home: Path) -> None:
    for mode in MODES:
        d = decide(mode=mode, tool="bash", args={"command": "rm -rf *"}, cwd=home)
        assert d.action == "deny" and d.hardline == "rm-rf-home", mode
        d = decide(mode=mode, tool="bash", args={"command": "rm -rf *", "cwd": ".."}, cwd=home / "proj")
        assert d.action == "deny" and d.hardline, mode
        d = decide(mode=mode, tool="bash", args={"command": "rm -rf ."}, cwd="/")
        assert d.action == "deny" and d.hardline == "rm-rf-root", mode
    assert decide(mode="auto", tool="bash", args={"command": "rm -rf *"}, cwd=tmp_path / "proj").action == "allow"


REDIRECT_PREFIX_AND_QUOTING = [
    ">/dev/null rm -rf /",
    "2>/dev/null rm -rf /",
    "</dev/null rm -rf /",
    "> out.txt sudo rm -rf /",
    "$'rm' -rf /",
    "$'\\x72m' -rf /",
    "{rm,-rf,/}",
    "sudo {rm,-rf,/}",
    "rm -rf {/,x}",
    "rm -rf /{usr,etc}/*",
]


@pytest.mark.parametrize("cmd", REDIRECT_PREFIX_AND_QUOTING)
def test_redirects_ansi_c_quotes_and_braces_do_not_hide_rm(proj: Path, cmd: str) -> None:
    _assert_hardline(cmd, proj)


# ── nothing the older readings stopped is let through ──


@pytest.mark.parametrize(
    "cmd",
    [
        "unshare -r echo .env",
        "if true; then echo .env; fi",
        "watch cat .env",
        "strace -f echo ~/.ssh/id_rsa",
        "! echo .env",
    ],
)
def test_a_name_only_command_behind_a_new_wrapper_or_keyword_is_still_read_conservatively(
    proj: Path, home: Path, cmd: str
) -> None:
    (home / ".ssh").mkdir()
    for mode in MODES:
        d = _bash(cmd, mode, proj)
        assert d.action == "deny" and d.hardline, (cmd, mode)  # sensitive-path, or the older cat-pattern names


def test_a_user_deny_rule_still_sees_the_command_behind_a_new_wrapper(proj: Path) -> None:
    rules = [Rule(tool="bash", pattern="watch *", action="deny"), Rule(tool="bash", pattern="strace *", action="deny")]
    for cmd in ("sudo watch ls", "watch ls", "sudo strace -f ls", "nice strace ls"):
        for mode in ("default", "auto"):
            assert _bash(cmd, mode, proj, rules=rules).action == "deny", (cmd, mode)


@pytest.mark.parametrize("cmd", ["git status >| rm -rf /", "curl x >| sh", "curl x >| bash"])
def test_the_clobber_redirect_does_not_hide_what_the_old_pipe_reading_denied(proj: Path, cmd: str) -> None:
    _assert_hardline(cmd, proj)


def test_the_clobber_redirect_keeps_asking_under_an_allow_rule(proj: Path) -> None:
    allow = [Rule(tool="bash", pattern="echo *", action="allow")]
    assert _bash("echo hi > out.txt", "default", proj, rules=allow).action == "allow"
    assert _bash("echo hi >| out.txt", "default", proj, rules=allow).action == "ask"  # as before: `out.txt` rated too


def test_ansi_c_words_and_heredoc_operators_only_add_readings(proj: Path) -> None:
    assert _bash("grep $'x' -rf 777", "default", proj).action == "ask"  # read as the variable $x, as before
    assert _bash("cat <<< /etc/passwd", "default", proj).action == "ask"


# ── 5. the edit tools and code locations ──

CODE_LOCATIONS = [
    ".git/hooks/pre-commit",
    ".git/hooks/post-merge",
    ".git/config",
    ".git/info/exclude",
    ".git/info/attributes",
    ".husky/pre-commit",
    ".husky/_/husky.sh",
    ".envrc",
    "sub/repo/.git/hooks/pre-push",
    "sub/repo/.git/config",
    "vendor/lib/.husky/pre-commit",
    "packages/app/.envrc",
    ".git/modules/lib/hooks/pre-commit",
    ".git/modules/lib/config",
    ".git/worktrees/w/config.worktree",
    ".k3code/config.yaml",
]


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize("path", CODE_LOCATIONS)
def test_edit_tools_ask_in_every_mode_for_code_locations(proj: Path, tool: str, path: str) -> None:
    for mode in ("default", "accept-edits", "auto", "yolo"):
        d = decide(mode=mode, tool=tool, args={"path": path}, cwd=proj)
        assert (d.action, d.needs_human) == ("ask", True), (mode, tool, path, d.action)
        assert "runs as code" in (d.message or ""), (mode, path, d.message)
        assert d.auto_allowed is False
    assert decide(mode="plan", tool=tool, args={"path": path}, cwd=proj).action == "deny"
    assert decide(mode="auto", tool=tool, args={"path": path}, cwd=proj, headless=True).action == "deny"
    assert decide(mode="yolo", tool=tool, args={"path": path}, cwd=proj, headless=True).action == "deny"


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore",
        ".github/workflows/ci.yml",
        "src/a.py",
        "hooks/pre-commit",
        ".gitattributes",
        "docs/.git-notes",
        ".envrc.md",
        ".husky.md",
    ],
)
def test_other_edits_keep_their_decisions(proj: Path, path: str) -> None:
    for mode in ("accept-edits", "auto", "yolo"):
        assert decide(mode=mode, tool="write", args={"path": path}, cwd=proj).action == "allow", (mode, path)
    assert decide(mode="default", tool="write", args={"path": path}, cwd=proj).action == "ask"


def test_a_symlink_into_a_hook_directory_is_still_a_code_location(proj: Path) -> None:
    (proj / ".git" / "hooks").mkdir(parents=True)
    (proj / "tools").symlink_to(proj / ".git" / "hooks")
    d = decide(mode="auto", tool="write", args={"path": "tools/pre-commit"}, cwd=proj)
    assert (d.action, d.needs_human) == ("ask", True)


def test_reading_a_hook_or_config_is_still_allowed(proj: Path) -> None:
    for path in (".git/hooks/pre-commit.sample", ".git/config", ".husky/pre-commit", ".envrc"):
        assert decide(mode="auto", tool="read", args={"path": path}, cwd=proj).action == "allow", path


@pytest.mark.parametrize("pattern", ["*", "**", "{proj}/**", "{proj}/*", "{proj}/.git/**", "{proj}/.git/*/*"])
def test_a_broad_edit_allow_rule_does_not_cover_code_locations(proj: Path, pattern: str) -> None:
    # K3NOX-106: only the builtin layer was checked, so any user or project `edit: allow` let a hook be planted
    rules = [Rule(tool="edit", pattern=pattern.format(proj=proj), action="allow")]
    for path in (".git/hooks/pre-commit", ".envrc", ".husky/pre-commit", ".git/config"):
        for mode in ("default", "accept-edits", "auto", "yolo"):
            d = decide(mode=mode, tool="write", args={"path": path}, cwd=proj, user_rules=rules)
            assert (d.action, d.needs_human) == ("ask", True), (pattern, path, mode, d.action)
            assert "runs as code" in (d.message or "")
    assert decide(mode="auto", tool="write", args={"path": "src/a.py"}, cwd=proj, user_rules=rules).action == "allow"


def test_an_explicit_allow_rule_for_a_hook_path_is_honoured(proj: Path) -> None:
    rules = [Rule(tool="edit", pattern=f"{proj}/.husky/*", action="allow")]
    d = decide(mode="auto", tool="write", args={"path": ".husky/pre-commit"}, cwd=proj, user_rules=rules)
    assert d.action == "allow"


# ── 6. .env protection ──


def _grep(arguments: dict, cwd: Path) -> dict:
    from k3code.tools import tool_grep

    return asyncio.run(tool_grep(arguments, cwd=cwd))


@pytest.fixture
def dotenv_project(proj: Path) -> Path:
    (proj / ".env").write_text("API_KEY=" + "dotenv-one\n")
    (proj / ".env.local").write_text("API_KEY=" + "dotenv-two\n")
    (proj / "sub").mkdir()
    (proj / "sub" / ".env").write_text("API_KEY=" + "dotenv-three\n")
    (proj / "src").mkdir()
    (proj / "src" / "a.py").write_text("API_KEY = read_setting('API_KEY')\n")
    return proj


@pytest.mark.parametrize("include", ["*", ".env", ".e*", ".*", "**", ["*.py", ".env"], "*.local", ".env.*"])
def test_the_grep_tool_never_searches_dotenv_files_whatever_the_include_glob(dotenv_project: Path, include) -> None:
    out = _grep({"pattern": "API_KEY", "include": include}, dotenv_project)
    text = out.get("matches", "")
    assert "dotenv-" not in text, (include, out)
    assert ".env" not in text, (include, out)


def test_the_grep_tool_still_finds_ordinary_files_with_a_wildcard_include(dotenv_project: Path) -> None:
    assert "src/a.py" in _grep({"pattern": "API_KEY", "include": "*"}, dotenv_project)["matches"]
    assert "src/a.py" in _grep({"pattern": "API_KEY", "include": "*.py"}, dotenv_project)["matches"]
    assert "src/a.py" in _grep({"pattern": "API_KEY"}, dotenv_project)["matches"]


def test_the_grep_tool_never_reaches_key_directories_under_a_home_root(home: Path) -> None:
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("THE-KEY-MATERIAL\n")
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text("THE-KEY-MATERIAL\n")
    (home / ".config" / "k3code").mkdir(parents=True)
    (home / ".config" / "k3code" / "env").write_text("THE-KEY-MATERIAL\n")
    (home / "notes.txt").write_text("THE-KEY-MATERIAL in prose\n")
    out = _grep({"pattern": "THE-KEY-MATERIAL", "include": "*"}, home)
    assert out["matches"].splitlines() == ["notes.txt", "1:THE-KEY-MATERIAL in prose"]


def test_the_python_fallback_skips_dotenv_files_too(dotenv_project: Path) -> None:
    from k3code.tools import _grep_fallback

    for include in (None, "*", ".env"):
        out = _grep_fallback("API_KEY", dotenv_project, include, None, dotenv_project)
        assert "dotenv-" not in out["matches"], include
        assert ".env" not in out["matches"], include
    assert "src/a.py" in _grep_fallback("API_KEY", dotenv_project, "*", None, dotenv_project)["matches"]


def test_a_variable_that_holds_a_credential_name_is_resolved_or_asked(proj: Path) -> None:
    (proj / ".env").write_text("K=1\n")
    _assert_hardline("f=.env; cat $f", proj)
    _assert_hardline('f=.env\ncat "$f"', proj)
    _assert_hardline("f=.env && tail -n 5 ${f}", proj)
    _assert_hardline("d=.en; cat ${d}v", proj)


# K3NOX-106: commands run under bash now, which decodes $'...' (dash left it literal): the credential checks read the
# decoded spelling too
ANSI_C_CREDENTIAL_READS = [
    r"cat $'\x2eenv'",
    r"cat $'\056env'",
    r"cat $'\x2e'env",
    r"head -5 $'\x2eenv'",
    r"grep -r KEY $'\x2eenv'",
    r"cp $'\x2eenv' /tmp/x",
    r"cat ~/$'\x2essh'/id_rsa",
]


@pytest.mark.parametrize("cmd", ANSI_C_CREDENTIAL_READS)
def test_an_ansi_c_quoted_credential_name_is_decoded_before_the_credential_checks(proj: Path, cmd: str) -> None:
    (proj / ".env").write_text("K=1\n")
    _assert_hardline(cmd, proj)


READ_WITH_EXPANSION = [
    "cat $(echo .e)nv",
    "cat `echo .e`nv",
    "head -5 $f",
    'tail -n 2 "$file"',
    "grep KEY $files",
    "grep -rn KEY $(git ls-files)",
    "sort $(ls)",
    "cp $src /tmp/x",
    "base64 ${target}",
    "sed -n 1p $f",
    "wc -l $(git ls-files '*.py')",
]


@pytest.mark.parametrize("cmd", READ_WITH_EXPANSION)
def test_a_read_command_argument_with_an_unresolved_expansion_needs_a_human(proj: Path, cmd: str) -> None:
    _assert_needs_human(cmd, proj)


@pytest.mark.parametrize(
    "cmd",
    [
        'grep "$pattern" src/a.py',
        'grep -n "$(date)" log.txt',
        'rg "$q" src',
        'sed -n "${n}p" a.py',
        'awk "$prog" a.txt',
        'grep -e "$p" -r src',
        "echo $f",
        "echo $(cat a.txt)",
        "ls $DIR",
        'git commit -m "$msg"',
        "pytest $ARGS",
        "curl $URL",
        "cat src/a.py",
        'cat "$PWD/README.md"',
    ],
)
def test_expansions_that_cannot_name_a_credential_are_not_over_asked(proj: Path, cmd: str) -> None:
    d = _bash(cmd, "auto", proj)
    assert not d.needs_human, (cmd, d.message)


# ── 7. .ENV on a case-insensitive file system ──


@pytest.mark.parametrize("name", [".ENV", ".Env", ".env", ".ENV.local", ".Env.PRODUCTION"])
def test_dotenv_names_are_matched_case_insensitively(proj: Path, name: str) -> None:
    assert sensitive_path(str(proj / name))
    for mode in ("default", "auto", "yolo", "plan"):
        for tool in ("read", "grep"):
            d = decide(mode=mode, tool=tool, args={"path": name, "pattern": "."}, cwd=proj)
            assert (d.action, d.hardline) == ("deny", "sensitive-path"), (mode, tool, name)
        d = _bash(f"cat {name}", mode, proj)  # `.env` itself is also caught by the older dotenv-cat pattern
        assert d.action == "deny" and d.hardline in ("sensitive-path", "dotenv-cat"), (mode, name)
    assert _bash(f"head -3 {name}", "auto", proj).action == "deny"


def test_templates_and_key_dirs_keep_their_case_insensitive_rules(proj: Path, home: Path) -> None:
    assert not sensitive_path(str(proj / ".ENV.EXAMPLE"))
    assert not sensitive_path(str(proj / ".Env.Sample"))
    assert decide(mode="auto", tool="read", args={"path": ".ENV.example"}, cwd=proj).action == "allow"
    assert sensitive_path(str(home / ".SSH" / "id_rsa"))
    assert sensitive_path(str(home / ".Aws" / "credentials"))
    assert _bash("cat ~/.SSH/ID_RSA", "default", proj).action == "deny"
    assert _bash("cat .EN?", "auto", proj).action == "deny"
    assert not sensitive_path(str(proj / "environment.md"))
    assert not sensitive_path(str(proj / "src" / "env.py"))


# ── 8. more catastrophes in the hardline ──

NEW_HARDLINE = [
    "echo x >/dev/sda",
    "echo x > /dev/sda1",
    "echo x >> /dev/nvme0n1",
    "cat img >| /dev/vda",
    "cat img 1<>/dev/hda",
    "echo x &>/dev/mmcblk0",
    "echo x > /dev/disk/by-id/ata-X",
    "echo x > /dev/xvda",
    "echo x > '/dev/sda'",
    "echo x > /dev/../dev/sda",
    "cp img /dev/sda",
    "sudo cp img.iso /dev/sdb",
    "mv img /dev/nvme0n1",
    "cat img | tee /dev/sda",
    "cat img | sudo tee -a /dev/vdb",
    "shred -n1 /dev/sda",
    "wipefs -a /dev/sda",
    "blkdiscard /dev/nvme0n1",
    "chmod -R 777 /",
    "chmod --recursive a+rwx /",
    "chmod -Rf 755 /",
    "sudo chown -R me:me /",
    "chown --recursive me /",
    "chown -R me /*",
    "chgrp -R staff /",
    "find / -delete",
    "find / -name '*.log' -delete",
    "sudo find / -type f -delete",
    "find /* -delete",
    "find / -mindepth 1 -maxdepth 1 -delete",
    "find ~ -delete",
    "find $HOME -delete",
    "find ~/ -type f -delete",
    "find ~ -depth -delete",
    "echo ~ | xargs rm -rf",
    "echo $HOME | sudo xargs rm -r",
    "printf '%s\\n' / | xargs rm -rf",
    "find ~ | xargs rm -rf",
    "find / -type f | xargs rm -rf",
    ":(){ :|:& };:",
    ":(){ :|: & };:",
    ":() { : | : & } ; :",
    "bomb(){ bomb|bomb& };bomb",
    "function bomb { bomb | bomb & }; bomb",
    "bash -c ':(){ :|:& };:'",
]


@pytest.mark.parametrize("cmd", NEW_HARDLINE)
def test_unambiguous_catastrophes_are_hardline_denied(proj: Path, cmd: str) -> None:
    _assert_hardline(cmd, proj)


def test_the_fork_bomb_rule_is_linear_and_still_catches_long_and_suffix_names() -> None:
    # K3NOX-106: the regex tried every start of a long word, so `echo aaaa...` (20k) took seconds on the event loop
    import time

    started = time.perf_counter()
    for text in ("echo " + "a" * 20_000, "a" * 20_000 + "() { b }", "f() { " + "a" * 20_000 + " | b }"):
        assert hardline.check(text, cwd="/work/proj", home="/home/user") is None
    assert time.perf_counter() - started < 1.0
    name = "a" * 300  # no length cap on the name: a long one is no way around the rule
    assert hardline.check(f"{name}(){{ {name}|{name}& }};{name}") == "fork-bomb"
    assert hardline.check("x.f(){ f|f& }; x.f") == "fork-bomb"  # as before: the body's name ends the word in front


NEW_HARDLINE_CONTROLS = [
    "echo x > /dev/null",
    "echo x > /dev/stderr",
    "echo x 2>/dev/null",
    "tee /dev/null",
    "cp a /dev/shm/b",
    "dd if=/dev/sda of=disk.img",
    "cat /dev/sda > disk.img",
    "lsblk /dev/sda",
    "smartctl -a /dev/sda",
    "fdisk -l /dev/sda",
    "hexdump -C -n 512 /dev/nvme0n1",
    "chmod -R 755 ./dist",
    "chmod -R u+w .",
    "chmod 600 /",
    "chown -R me .",
    "chown me /",
    "find . -name x -delete",
    "find build -delete",
    "find /tmp/x -delete",
    "find / -name x",
    "find / -name x -print",
    "foo(){ foo2 | bar & }; foo",
    "ls | wc -l &",
    "echo ':() is a function'",
]


@pytest.mark.parametrize("cmd", NEW_HARDLINE_CONTROLS)
def test_nearby_ordinary_commands_are_not_hardline(cmd: str) -> None:
    assert hardline.check(cmd, cwd="/work/proj", home="/home/user") is None, cmd


FORCE_PUSH_WITHOUT_REFSPEC = [
    "git push --force",
    "git push -f",
    "git push --force origin",
    "git push origin --force",
    "git push --force-with-lease",
    "git push --force-with-lease origin",
    "git push --force-if-includes",
    "git -C repo push -f",
    "cd repo && git push --force",
    "sudo git push -f origin",
]


@pytest.mark.parametrize("cmd", FORCE_PUSH_WITHOUT_REFSPEC)
def test_a_force_push_without_a_refspec_always_goes_to_a_human(proj: Path, cmd: str) -> None:
    assert hardline.check(cmd, cwd=str(proj), home="/home/user") is None  # not a hardline: a person decides
    _assert_needs_human(cmd, proj)
    allow = [Rule(tool="bash", pattern="git push *", action="allow")]
    d = _bash(cmd, "auto", proj, rules=allow)
    assert (d.action, d.needs_human) == ("ask", True), cmd
    assert _bash(cmd, "yolo", proj).action == "ask", cmd  # every mode, yolo included
    assert _bash(cmd, "yolo", proj, headless=True).action == "deny", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "git push",
        "git push origin",
        "git push origin feature",
        "git push --force origin feature",
        "git push -f origin feature",
        "git push --force-with-lease origin feature",
        "git push origin +feature",
        "git push --set-upstream origin feature",
        "git push --tags",
        "git pull --force",
        "git commit -m 'git push --force'",
    ],
)
def test_pushes_with_a_refspec_and_other_commands_keep_their_decisions(proj: Path, cmd: str) -> None:
    d = _bash(cmd, "auto", proj)
    assert (d.action, d.needs_human) == ("allow", False), cmd


def test_force_main_pushes_are_still_hardline_denied(proj: Path) -> None:
    _assert_hardline("git push --force origin main", proj)
    _assert_hardline("git push origin +master", proj)


def test_a_malformed_user_hardline_regex_never_raises_and_the_valid_ones_still_apply(
    proj: Path, caplog: pytest.LogCaptureFixture
) -> None:
    extra = ["rm -rf (", "[unclosed", r"\bshutdown\b", "*bad"]
    with caplog.at_level(logging.WARNING):
        for mode in MODES:
            d = decide(mode=mode, tool="bash", args={"command": "echo hi"}, cwd=proj, hardline_extra=extra)
            assert d.action in ("allow", "ask"), mode
            d = decide(mode=mode, tool="bash", args={"command": "shutdown -h now"}, cwd=proj, hardline_extra=extra)
            assert (d.action, d.hardline) == ("deny", "config-hardline-2"), mode
        assert hardline.check("echo hi", ["("]) is None
    assert any("invalid" in r.getMessage().lower() for r in caplog.records)
    assert sum("[unclosed" in r.getMessage() for r in caplog.records) == 1  # warned once, not per command


def test_a_malformed_user_hardline_regex_is_matched_as_the_literal_text_it_was_written_as(proj: Path) -> None:
    d = decide(mode="yolo", tool="bash", args={"command": "echo 'foo(bar'"}, cwd=proj, hardline_extra=["foo(bar"])
    assert (d.action, d.hardline) == ("deny", "config-hardline-0")


def test_non_string_hardline_entries_do_not_crash_either(proj: Path) -> None:
    d = decide(mode="auto", tool="bash", args={"command": "echo 42"}, cwd=proj, hardline_extra=[42])  # type: ignore[list-item]
    assert d.action in ("allow", "deny")


# ── 9. an "always" approval never persists a secret ──

# Secret-shaped values are built at run time: no credential-looking literal sits in this file.
_TOKEN = "sk-" + "x" * 30
_GH = "ghp_" + "a" * 36
_AWS = "AKIA" + "Q" * 16
_PW = "p" * 12 + "w9"
SECRET_COMMANDS = [
    f"npx tool --token {_TOKEN}",
    f"docker run -e AWS_SECRET_ACCESS_KEY={_PW} img",
    f"uvx tool --password {_PW}",
    f"npx tool --token={_PW}",
    f"uv run script.py --api-key {_GH}",
    f"npx tool --id {_AWS}",
]
_SECRET_PARTS = (_TOKEN, _GH, _AWS, _PW)


@pytest.mark.parametrize("cmd", SECRET_COMMANDS)
def test_always_approval_does_not_persist_a_command_that_carries_a_secret(proj: Path, cmd: str) -> None:
    d = _bash(cmd, "default", proj)
    rules = suggest_rules("bash", d)
    assert not any(part in r.pattern for r in rules for part in _SECRET_PARTS), rules
    from k3code.redact import scrub_text

    assert all(scrub_text(r.pattern) == r.pattern for r in rules), rules


def test_the_exact_pattern_helper_refuses_a_secret_and_the_state_writer_double_checks(proj: Path) -> None:
    from k3code.permissions.engine import exact_rule_pattern
    from k3code.permissions.state import persist_rules

    assert exact_rule_pattern(f"npx tool --token {_TOKEN}") is None
    assert exact_rule_pattern("npx eslint .") == "npx eslint ."
    cfg = proj / ".k3code" / "config.yaml"
    persist_rules(
        cfg,
        [
            Rule(tool="bash", pattern=f"npx tool --token {_TOKEN}", action="allow"),
            Rule(tool="bash", pattern="npm test *", action="allow"),
        ],
    )
    text = cfg.read_text()
    assert _TOKEN not in text and "npm test *" in text


@pytest.mark.parametrize(
    ("cmd", "expected"), [("npx eslint .", ["npx eslint ."]), ("git commit -m x", ["git commit *"])]
)
def test_ordinary_always_rules_are_unchanged(proj: Path, cmd: str, expected: list[str]) -> None:
    assert [r.pattern for r in suggest_rules("bash", _bash(cmd, "default", proj))] == expected
