"""The hardline sees through shell reserved words, braces, ``!``, function definitions and ``busybox``.

``{ rm -rf ~; }``, ``if true; then env; fi``, ``! rm -rf ~``, ``busybox rm -rf ~`` and the like were read as commands
named ``{``, ``then``, ``!`` or ``busybox``, so the rm and environment-dump checks never saw the command that runs,
and auto/yolo allowed it. So were ``cd / && rm -rf *`` (a relative target after a cd to /), ``find ~ -delete`` and
``echo ~ | xargs rm -rf``.
"""

from __future__ import annotations

import itertools
import random
from pathlib import Path

import pytest

from k3code.permissions import decide, hardline

MUST_DENY = [
    "{ rm -rf ~; }",
    "{ { rm -rf ~; }; }",
    "if true; then rm -rf ~; fi",
    "while true; do rm -rf ~; done",
    "until false; do rm -rf ~; done",
    "for i in 1 2; do rm -rf ~; done",
    "! rm -rf ~",
    "coproc rm -rf ~",
    "select x in a b; do rm -rf ~; done",
    "function f { rm -rf ~; }; f",
    "f() { rm -rf ~; }; f",
    "if true; then rm -rf $HOME; fi",
    "busybox rm -rf ~",
    "cd / && rm -rf *",
    "if true; then env; fi",
    "while true; do printenv; done",
    "find ~ -delete",
    "find $HOME -exec rm -rf {} +",
    "find / -delete",
    "echo ~ | xargs rm -rf",
    "echo $HOME | xargs rm -rf",
    "{ cd / && rm -rf *; }",
    # the same gap one step further: launchers, heredocs and wrappers behind a reserved word
    "if true; then bash -c 'rm -rf ~'; fi",
    "{ bash -c 'rm -rf ~'; }",
    "{ bash <<EOF\nrm -rf ~\nEOF\n}",
    "then eval rm -rf ~",
    "busybox sh -c 'rm -rf ~'",
    "sudo busybox env",
    "time { rm -rf ~; }",
    "coproc NAME { rm -rf ~; }",
    "if false; then :; elif true; then rm -rf ~; fi",
    "if false; then :; else printenv; fi",
    "if ! rm -rf ~; then :; fi",
    "cd / && rm -rf usr",
    "cd ~ && rm -rf *",
    "cd /tmp && cd .. && rm -rf ./*",
    "cd / && find . -delete",
    "cd / && find -delete",
    "find ~ -name '*.tmp' -execdir /bin/rm {} +",
    "find -L / -exec sudo rm -f {} \\;",
    "echo ~ | sudo xargs -0 rm -rf",
    "find ~ -maxdepth 1 | xargs rm -r",
    "rm -rf /./*",
]

MUST_NOT_DENY = [
    "rm -rf build",
    "rm -rf ./node_modules",
    "rm -rf ./dist/",
    "find . -name '*.pyc' -delete",
    "find . -exec grep x {} +",
    "echo hi | xargs ls",
    "if [ -f x ]; then echo ok; fi",
    "for f in *.py; do wc -l $f; done",
    "git clean -n",
    "while read l; do echo $l; done < file",
    "{ echo a; echo b; }",
    "! grep -q x file",
    "busybox ls",
    "xargs -n1 echo < list.txt",
    "find ~/projects -name '*.md'",
    # look-alikes of the new checks
    "busybox --list",
    "cd build && rm -rf *",
    "cd /tmp/scratch && rm -rf *",
    "cd src && find . -delete",
    "find ~ -name '*.md'",
    "find ~/projects/x -delete",
    "find / -name x -print",
    "echo ~ | xargs ls -la",
    "echo ~ | xargs rm -f",
    "echo build | xargs rm -rf",
    "echo ~ | rm -rf xargs-out",
    "ls | xargs rm -rf",
    "for d in a b; do rm -rf $d; done",
    "case $x in a) echo a;; esac",
    "function f { echo hi; }; f",
]


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.mark.parametrize("command", MUST_DENY)
def test_denied(command: str, proj: Path) -> None:
    assert hardline.check(command, cwd=str(proj)) is not None, command


@pytest.mark.parametrize("command", MUST_NOT_DENY)
def test_not_hardline(command: str, proj: Path) -> None:
    assert hardline.check(command, cwd=str(proj)) is None, command


@pytest.mark.parametrize("mode", ["default", "accept-edits", "auto", "yolo"])
@pytest.mark.parametrize("command", MUST_DENY)
def test_decide_denies_in_every_mode(command: str, mode: str, proj: Path) -> None:
    assert decide(mode=mode, tool="bash", args={"command": command}, cwd=proj).action == "deny"


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("cd / && rm -rf *", "rm-rf-root"),
        ("{ cd / && rm -rf *; }", "rm-rf-root"),
        ("cd ~ && rm -rf *", "rm-rf-home"),
        ("if true; then rm -rf ~; fi", "rm-rf-home"),
        ("busybox rm -rf /", "rm-rf-root"),
        ("while true; do printenv; done", "env-dump"),
        ("find ~ -delete", "rm-rf-home"),
        ("find / -delete", "rm-rf-root"),
        ("echo $HOME | xargs rm -rf", "rm-rf-home"),
    ],
)
def test_rule_names(command: str, rule: str, proj: Path) -> None:
    assert hardline.check(command, cwd=str(proj)) == rule


def test_find_above_home_is_home(proj: Path) -> None:
    home = "/var/home/someone"
    assert hardline.check("find /var/home -delete", cwd=str(proj), home=home) == "rm-rf-home"
    assert hardline.check("find /var/home/someone/code -delete", cwd=str(proj), home=home) is None


#: Constructs that run the command ``{}`` stands for; each can hold any other.
NESTINGS = [
    "{{ {}; }}",
    "if true; then {}; fi",
    "if {}; then :; fi",
    "if false; then :; elif true; then {}; fi",
    "if false; then :; else {}; fi",
    "while true; do {}; done",
    "while {}; do :; done",
    "until false; do {}; done",
    "for i in 1 2; do {}; done",
    "select x in a b; do {}; done",
    "case x in x) {};; esac",
    "! {}",
    "time {}",
    "coproc {}",
    "coproc NAME {{ {}; }}",
    "function f {{ {}; }}; f",
    "f() {{ {}; }}; f",
    "( {} )",
    "bash -c '{}'",
]
#: Simple commands only: these go innermost.
SIMPLE_WRAPPERS = ["{}", "busybox {}", "sudo {}", "env X=1 {}"]
DENIED = ["rm -rf ~", "env", "printenv", "rm -rf $HOME", "find ~ -delete"]
INNOCENT = ["rm -rf build", "echo hi", "find . -name x -delete", "ls -la"]


def _nestings() -> list[tuple[str, ...]]:
    pairs = list(itertools.product(NESTINGS, repeat=2))
    rng = random.Random(4242)  # fixed: the same cases on every run
    deeper = [tuple(rng.choice(NESTINGS) for _ in range(rng.randint(3, 5))) for _ in range(150)]
    return [(n,) for n in NESTINGS] + pairs + deeper


def _wrap(inner: str, simple: str, layers: tuple[str, ...]) -> str:
    command = simple.format(inner)
    for layer in reversed(layers):
        if "'" in layer and ("'" in command or " " not in command):
            # a quote inside bash -c '...' would end it; a one-word payload (`bash -c env`) is a separate, older gap:
            # only an argument with a blank or a shell character is read as a command (see _launcher_payloads)
            continue
        command = layer.format(command)
    return command


def test_nested_denied_commands_stay_denied(proj: Path) -> None:
    misses = []
    for k, layers in enumerate(_nestings()):
        inner = DENIED[k % len(DENIED)]
        simple = SIMPLE_WRAPPERS[k % len(SIMPLE_WRAPPERS)]
        command = _wrap(inner, simple, layers)
        if hardline.check(command, cwd=str(proj)) is None:
            misses.append(command)
    assert not misses, misses[:10]


def test_nested_innocent_commands_stay_innocent(proj: Path) -> None:
    hits = []
    for k, layers in enumerate(_nestings()):
        inner = INNOCENT[k % len(INNOCENT)]
        simple = SIMPLE_WRAPPERS[k % len(SIMPLE_WRAPPERS)]
        command = _wrap(inner, simple, layers)
        if hardline.check(command, cwd=str(proj)) is not None:
            hits.append(command)
    assert not hits, hits[:10]
