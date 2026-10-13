"""The hardline ``env-dump`` rule: the model may not print the environment (secrets), but ``env VAR=x cmd`` (a wrapper
that runs a command) and ``printenv NAME`` (one ordinary variable) are everyday commands and pass. Every way the rule
could be walked around through ``env`` itself is pinned here too: only a command that runs something is let through."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code.permissions import decide
from k3code.permissions.hardline import HARDLINE_NAMES, check, normalize_argv
from k3code.redact import is_secret_key

ALLOWED = [
    # env as a wrapper: it runs the command, which the normal rules then judge
    "env NODE_ENV=production echo hi",
    "env PYTHONPATH=. python3 -c 'print(1)'",
    "env CI=true npm test",
    "env A=b C=d cmd",
    "env A=b cmd > out.txt",
    "env A=b cmd | grep x",
    "env A='x y' cmd",
    "/usr/bin/env A=b cmd",
    "env echo hi",
    "env -i A=b cmd",
    "env -u FOO cmd",
    "env -C /tmp cmd",
    "env -0 echo hi",
    "env -- A=1 echo hi",
    "env -S 'A=1 echo hi'",
    "env -- A=1 B=2 echo $HOME",
    "env -S 'A=1 B=2 npm test'",
    "sudo env A=b cmd",
    "nice env A=b cmd",
    "FOO=1 env A=b cmd",
    "echo hi && env CI=true npm test",
    # printenv NAME: one ordinary variable
    "printenv PATH",
    "printenv HOME",
    "printenv NODE_ENV",
    "printenv HOME PATH",
    "printenv -0 PATH",
    "printenv PATH 2>&1",
    "printenv PATH > /dev/null",
    "printenv PATH | head -1",
    "env FOO=1 printenv FOO",
    "env A=b printenv HOME",
    "/usr/bin/printenv LANG",
    "sudo printenv SHELL",
    # not a dump at all
    "echo $PATH",
    "FOO=1 cmd",
    "FOO=1 printenv FOO",
]

DENIED = [
    # bare env / printenv, with options only, in any wrapper, pipeline, redirect or substitution
    "env",
    "env -0",
    "env --null",
    "env -u FOO",
    "env --unset=FOO",
    "env -i",
    "env --",
    "env -i A=b",
    "env A=b",
    "env A=b C=d",
    "env -C /tmp",
    "printenv",
    "printenv -0",
    "printenv --null",
    "printenv --",
    "sudo env",
    "sudo -u root env",
    "/usr/bin/env",
    "/usr/bin/printenv",
    "FOO=1 env",
    "nice env",
    "nice -n 5 env",
    "timeout 5 env",
    "command env",
    "xargs env",
    "env | grep -i path",
    "env > dump.txt",
    "env 2>&1",
    "printenv > dump.txt",
    "(env)",
    "$(env)",
    "`env`",
    "echo hi; env",
    "echo hi && printenv",
    "bash -c 'echo hi; env'",
    "bash -c 'env -i'",
    "bash -c 'printenv GITHUB_TOKEN'",
    "ssh host env",
    # a dump through the wrapper
    "env A=b env",
    "env A=b printenv",
    "env -i env",
    "env -i printenv",
    "env -i A=b printenv",
    "sudo env A=b env",
    "xargs printenv",
    # env with nothing to run, but spelled so that the assignments look like a command
    "env -- A=1",
    "env -S 'A=1'",
    "env --split-string=A=1",
    "env -S printenv",
    "env -S env",
    "sudo -u root env -- A=1",
    "nice env -S 'A=1'",
    # a command word that is an expansion may be empty: env then prints
    "env $X",
    'env "$@"',
    "env A=b $X",
    "env A=b ${X}",
    "env A=b $(echo printenv)",
    "env A=b `echo env`",
    "env -- A=1 $X",
    "env -- A=1 B=2 $(echo printenv)",
    "env -S 'A=1 $X'",
    "env --split-string='A=1 ${X}'",
    "sudo env -- A=1 $(echo printenv)",
    "nice env -- A=1 $X",
    "sudo -u root env $X",
    # options that take a value must not hide the command
    "env -P /usr/bin printenv",
    "env -a x printenv",
    "env --chdir /tmp printenv",
    "env --unset FOO printenv",
    # printenv of a credential, or of a name that cannot be read
    "printenv GITHUB_TOKEN",
    "printenv github_token",
    "printenv OPENAI_API_KEY",
    "printenv AWS_SECRET_ACCESS_KEY",
    "printenv DB_PASSWORD",
    "printenv DB_PASS",
    "printenv PASS",
    "printenv MY_PASSPHRASE",
    "printenv GOOGLE_APPLICATION_CREDENTIALS",
    "printenv DATABASE_URL",
    "printenv REDIS_URL",
    "printenv SENTRY_DSN",
    "printenv SSH_AUTH_SOCK",
    "printenv HTTP_COOKIE",
    "printenv PATH GITHUB_TOKEN",
    "printenv GITHUB_TOKEN PATH",
    "printenv -0 GITHUB_TOKEN",
    "printenv $NAME",
    'printenv "$NAME"',
    "printenv ${NAME}",
    "printenv $(echo GITHUB_TOKEN)",
    "printenv 'GITHUB_*'",
    "printenv GITHUB_TOKE?",
    "name=GITHUB_TOKEN; printenv $name",
    "env A=b printenv GITHUB_TOKEN",
    "env FOO=1 printenv DB_PASSWORD",
    "env -i printenv API_KEY",
    "sudo printenv GITHUB_TOKEN",
    # the other dumps stay, and so does /proc
    "export -p",
    "set",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_everyday_env_and_printenv_commands_pass(command: str) -> None:
    assert check(command, cwd="/tmp/proj", home="/home/u") is None, command


@pytest.mark.parametrize("command", DENIED)
def test_every_way_to_print_the_environment_is_still_denied(command: str) -> None:
    assert check(command, cwd="/tmp/proj", home="/home/u") is not None, command


@pytest.mark.parametrize("mode", ["default", "accept-edits", "auto", "yolo"])
def test_the_engine_denies_a_dump_in_every_mode_and_lets_a_wrapper_through(mode: str, tmp_path: Path) -> None:
    def hardline(command: str) -> str | None:
        return decide(mode=mode, tool="bash", args={"command": command}, cwd=tmp_path).hardline

    for command in ("env", "printenv", "env A=b env", "env -- A=1", "env $X", "printenv GITHUB_TOKEN"):
        assert hardline(command) is not None, (mode, command)
    for command in ("env CI=true npm test", "printenv PATH", "env FOO=1 printenv FOO"):
        assert hardline(command) is None, (mode, command)


def test_the_denial_says_what_to_write_instead(tmp_path: Path) -> None:
    d = decide(mode="yolo", tool="bash", args={"command": "env -i"}, cwd=tmp_path)
    assert d.action == "deny" and d.hardline == "env-dump"
    assert d.message is not None and d.message.startswith("Hardline deny (env-dump): env -i")
    assert "VAR=x cmd" in d.message and "printenv NAME" in d.message
    # the other rules keep their short message
    other = decide(mode="yolo", tool="bash", args={"command": "rm -rf /"}, cwd=tmp_path)
    assert other.message is not None and "VAR=x cmd" not in other.message


def test_env_stays_the_command_when_it_has_nothing_to_run() -> None:
    for argv in (
        ["env"],
        ["env", "-i"],
        ["env", "--", "A=1"],
        ["env", "-S", "A=1"],
        ["env", "$X"],
        ["env", "A=1", "$X"],
        [
            "env",
            "--",
            "A=1",
            "$X",
        ],  # the assignment is dropped on the next round: the expansion must not become the command
        ["env", "-S", "A=1 $X"],
        ["env", "--", "A=1", "B=2", "$(echo printenv)"],
    ):
        assert normalize_argv(argv)[0] == "env", argv
    assert normalize_argv(["env", "A=1", "cmd", "x"]) == ["cmd", "x"]
    assert normalize_argv(["env", "-i", "-u", "A", "B=1", "cmd"]) == ["cmd"]
    assert normalize_argv(["env", "--", "A=1", "cmd"]) == ["cmd"]
    assert normalize_argv(["env", "--", "A=1", "B=2", "cmd", "$X"]) == ["cmd", "$X"]
    assert normalize_argv(["env", "-S", "A=1 B=2 cmd"]) == ["cmd"]


def test_the_rule_is_still_listed_for_display() -> None:
    assert HARDLINE_NAMES.count("env-dump") == 1  # /permissions and the setup wizard list the rules by name


@pytest.mark.parametrize(
    "name",
    [
        "API_KEY",
        "GITHUB_TOKEN",
        "CLIENT_SECRET",
        "DB_PASSWORD",
        "DB_PASSWD",
        "DB_PWD",
        "AWS_CREDENTIALS",
        "SESSION_COOKIE",
        "SSH_AUTH_SOCK",
        "Authorization",
        "pgpassword",
    ],
)
def test_printenv_refuses_every_name_the_redaction_calls_a_secret(name: str) -> None:
    assert is_secret_key(name)  # the redaction's own list, so the two cannot drift apart
    assert check(f"printenv {name}") == "env-dump", name


@pytest.mark.parametrize("name", ["DB_PASS", "PASS", "DATABASE_URL", "SENTRY_DSN", "MY_PASSPHRASE"])
def test_printenv_also_refuses_the_names_the_redaction_misses(name: str) -> None:
    # a whole-word PASS, and the URL / DSN a password is written into
    assert check(f"printenv {name}") == "env-dump", name


def test_proc_environ_stays_denied_whatever_env_does(tmp_path: Path) -> None:
    for command in ("cat /proc/self/environ", "env A=b cat /proc/self/environ", "tr '\\0' '\\n' < /proc/self/environ"):
        d = decide(mode="yolo", tool="bash", args={"command": command}, cwd=tmp_path)
        assert d.action == "deny" and d.hardline == "sensitive-path", command


@pytest.mark.parametrize("name", ["PATH", "HOME", "USER", "SHELL", "LANG", "TERM", "CI", "NODE_ENV", "GIT_AUTHOR_NAME"])
def test_printenv_prints_an_ordinary_name(name: str) -> None:
    assert not is_secret_key(name)
    assert check(f"printenv {name}") is None, name
