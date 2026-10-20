"""A check looks up at most 32 ``~name`` users, and a ``~name`` it did not look up fails closed where it matters.

Every distinct ``~name`` was a user database lookup (``getpwnam``; with sssd or LDAP a network round trip), three per
word in ``decide``, which runs synchronously in the daemon: ``cat ~u0/x ... ~u7999/x`` took seconds. A check (one
``hardline.check`` or one ``decide``) now gives the first 32 distinct names on the line a lookup each; ``~root`` and the
user the daemon runs as cost none. A name past those is not looked up: as a delete target it is refused
(``scan-budget``: ``~bin`` is ``/bin``), a ``cd`` to it is one directory too many (``scan-depth``), a credential name
below it is refused (``sensitive-path``), and any other word with it stays as written.
"""

from __future__ import annotations

import pwd
from pathlib import Path
from types import SimpleNamespace

import pytest

from k3code.permissions import decide, hardline

HOME = "/home/dev"
DEMO = f"{HOME}/projects/demo"
JUNK = " ".join(f"~zzq{i}/x" for i in range(40))
MODES = ["default", "accept-edits", "auto", "yolo"]


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The names ``pwd.getpwnam`` is asked for: ``bin`` lives in ``/bin``, every other name is unknown."""
    calls: list[str] = []

    def getpwnam(name: str) -> SimpleNamespace:
        calls.append(name)
        if name == "bin":
            return SimpleNamespace(pw_dir="/bin")
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    return calls


def _own() -> str:
    name = hardline._own_name()
    assert name
    return name


def _check(command: str) -> str | None:
    return hardline.check(command, cwd=DEMO, home=HOME)


def _decide(command: str, mode: str, cwd: Path):
    return decide(mode=mode, tool="bash", args={"command": command}, cwd=cwd)


MANY = "cat " + " ".join(f"~u{i}/x" for i in range(8000))


def test_a_check_looks_up_at_most_32_users(lookups: list[str]) -> None:
    assert _check(MANY + "; rm -rf ~u7999/x") == "scan-budget"
    assert len(lookups) <= 32


def test_a_decision_looks_up_at_most_32_users(lookups: list[str], tmp_path: Path) -> None:
    _decide(MANY, "default", tmp_path)
    assert 0 < len(lookups) <= 32
    assert len(set(lookups)) == len(lookups)  # each name once


def test_root_and_the_own_user_cost_no_lookup(lookups: list[str], tmp_path: Path) -> None:
    command = "cat " + " ".join(f"~root/x{i} ~{_own()}/y{i}" for i in range(100))
    _decide(command, "default", tmp_path)
    assert _check(command + "; rm -rf ~root") == "rm-rf-root"
    assert _check(f"rm -rf ~{_own()}") == "rm-rf-home"  # the own user's home is the check's home
    assert lookups == []


def test_a_repeated_name_costs_one_lookup(lookups: list[str], tmp_path: Path) -> None:
    _decide("cat ~u1/x ~u1/y ~u1/z; ls ~u1; cd ~u1/w", "default", tmp_path)
    assert lookups == ["u1"]


def test_the_budget_is_per_check_not_per_process(lookups: list[str]) -> None:
    assert _check(JUNK) is None
    assert _check("rm -rf ~bin") == "rm-rf-root"  # a fresh budget: ~bin is looked up and is /bin
    assert _check("cat ~zzq0/x; rm -rf ~bin") == "rm-rf-root"  # inside the budget


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        (f"cat {JUNK}; rm -rf ~bin", "scan-budget"),  # 40 junk names spend the budget first
        (f"cat {JUNK}; rm -rf ~zzq99", "scan-budget"),
        (f"cat {JUNK}; rm -rf ~bin/x", "scan-budget"),
        (f"cat {JUNK}; find ~bin -delete", "scan-budget"),
        (f"cat {JUNK}; echo ~bin | xargs rm -rf", "scan-budget"),
        (f"cat {JUNK}; rm -rf ~root", "rm-rf-root"),  # root costs no lookup
        (f"cat {JUNK}; cd ~bin; rm -rf *", "scan-depth"),  # where the cd goes is unknown
        (f"cat {JUNK}; pushd ~bin; rm -rf *", "scan-depth"),
    ],
)
def test_a_name_past_the_budget_fails_closed(lookups: list[str], tmp_path: Path, command: str, rule: str) -> None:
    assert _check(command) == rule
    for mode in MODES:
        dec = _decide(command, mode, tmp_path)
        assert (dec.action, dec.hardline) == ("deny", rule), mode


@pytest.mark.parametrize("command", [f"cat {JUNK}", "rm -rf ~zzq1/build", "rm -rf ~zzq1", f"cat {JUNK}; ls ~zzq99"])
def test_other_uses_and_unknown_users_stay_harmless(lookups: list[str], tmp_path: Path, command: str) -> None:
    assert _check(command) is None
    for mode in MODES:
        assert _decide(command, mode, tmp_path).hardline is None, mode
    if not command.startswith("rm"):
        assert all(_decide(command, mode, tmp_path).action == "allow" for mode in MODES)


@pytest.mark.parametrize("leaf", [".ssh/id_rsa", ".env", "proc/self/environ"])
def test_a_credential_name_below_a_name_past_the_budget_is_refused(
    lookups: list[str], tmp_path: Path, leaf: str
) -> None:
    command = f"head {JUNK} ~nobody/{leaf}"  # ~nobody may be / (or the daemon's home): it was not looked up
    for mode in MODES:
        dec = _decide(command, mode, tmp_path)
        assert (dec.action, dec.hardline) == ("deny", "sensitive-path"), mode


def test_expand_user_follows_the_budget(lookups: list[str]) -> None:
    assert hardline.expand_user("~bin/x") == "/bin/x"  # outside a check: looked up
    assert hardline.expand_user("~zzq1/x") == "~zzq1/x"  # unknown: as written
    with hardline.lookup_scope():
        for i in range(32):
            hardline.expand_user(f"~j{i}")
        assert hardline.expand_user("~bin/x") == "~bin/x"  # past the budget: as written
        assert hardline.past_budget("~bin/x")
        assert hardline.expand_user("~j3/x") == "~j3/x"  # looked up already (unknown): free
        assert hardline.expand_user("~root/x") == "/root/x"
    assert hardline.expand_user("~bin") == "/bin"  # the scope is closed
