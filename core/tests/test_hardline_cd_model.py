"""The hardline reads a relative delete target in every directory the line may be in, and reads ``sh -c WORD``.

A ``cd`` that never takes effect (it fails, or runs in a subshell, a pipeline stage or the background) moved the
hardline's idea of the current directory away from ``/`` all the same, so ``cd /; cd /nonexistent; rm -rf *`` passed and
deleted ``/*``. A relative target is now read in the cwd and in every directory a cd on the line went to. ``rm -rf
/usr/*``, ``rm -rf /home/*`` and ``rm -rf ../..`` (from a folder two below home) passed as well, and ``bash -c env``
leaked the environment because a one-word ``-c`` payload was not read as code. Three checks also took time that grew
with the square of the command's length.
"""

from __future__ import annotations

import re

import pytest

from k3code.permissions import hardline

HOME = "/home/dev"
DEMO = f"{HOME}/projects/demo"


def _check(command: str, cwd: str = DEMO) -> str | None:
    return hardline.check(command, cwd=cwd, home=HOME)


CD_DENIED = [
    ("cd /; cd /nonexistent; rm -rf *", "rm-rf-root"),  # the second cd fails
    ("cd / && (cd tmp) && rm -rf *", "rm-rf-root"),  # a subshell's cd
    ("cd /; cd tmp & rm -rf *", "rm-rf-root"),  # a background cd
    ("cd /; cd tmp | true; rm -rf *", "rm-rf-root"),  # a pipeline stage's cd
    ("cd /; pushd tmp; popd; rm -rf *", "rm-rf-root"),
    ("cd /; pushd tmp; popd; cd usr; rm -rf *", "rm-rf-root"),  # popd went back to /
    ("cd ~; cd $X; rm -rf *", "rm-rf-home"),  # unset X makes it a plain `cd`
    ("cd $X; rm -rf *", "rm-rf-home"),
    ("cd /usr && rm -rf *", "rm-rf-root"),
    ("cd ~/.. && rm -rf *", "rm-rf-home"),
    ("cd .. && cd .. && rm -rf *", "rm-rf-home"),
    ("cd /tmp; cd /; cd tmp; rm -rf $PWD", "rm-rf-root"),
    ("cd /; cd tmp; find . -delete", "rm-rf-root"),
    ("cd /; cd tmp; echo usr | xargs rm -rf", "rm-rf-root"),
]

CD_ALLOWED = [
    "cd /tmp && rm -rf build",
    "cd build && rm -rf *",
    "cd /tmp; cd work; rm -rf out",
    "cd /usr; ls",
    "cd ~/projects/x && rm -rf dist",
    "cd /; cd tmp; rm -rf /tmp/x",
]


def test_a_directory_the_line_was_in_stays_possible() -> None:
    # over-denied on purpose: which separator joins the commands is not followed, so the rm is read in / although
    # `&&` keeps it from running when the cd fails
    assert _check("cd /; ls; cd /tmp/x && rm -rf ./*") == "rm-rf-root"
    assert _check("cd build && rm -rf *", cwd=HOME) == "rm-rf-home"


@pytest.mark.parametrize(("command", "rule"), CD_DENIED)
def test_a_relative_target_is_read_in_every_directory_the_line_cd_into(command: str, rule: str) -> None:
    assert _check(command) == rule


@pytest.mark.parametrize("command", CD_ALLOWED)
def test_cd_then_delete_inside_a_project_is_not_hardline(command: str) -> None:
    assert _check(command) is None


TARGETS = [
    ("rm -rf /usr/*", DEMO, "rm-rf-root"),
    ("rm -rf /opt/*", DEMO, "rm-rf-root"),
    ("rm -rf /home/*", DEMO, "rm-rf-home"),
    ("rm -rf ../..", DEMO, "rm-rf-home"),  # deletes ~
    ("rm -rf ../../*", DEMO, "rm-rf-home"),
    ("rm -rf *", HOME, "rm-rf-home"),
    ("rm -rf usr", "/", "rm-rf-root"),
    ("rm -rf /tmp/foo/*", DEMO, None),
    ("rm -rf ../other", DEMO, None),
    ("rm -rf *", DEMO, None),
    ("rm -rf build", None, None),
]


@pytest.mark.parametrize(("command", "cwd", "rule"), TARGETS)
def test_a_relative_target_is_read_in_the_cwd_and_a_folder_glob_by_its_folder(
    command: str, cwd: str | None, rule: str | None
) -> None:
    assert hardline.check(command, cwd=cwd, home=HOME) == rule


@pytest.mark.parametrize(
    "command",
    [
        "bash -c env",
        "busybox ash -c env",
        "sh -c printenv",
        "bash -c 'env'",
        "bash -lc env",
        "bash -o pipefail -c env",
        "sudo sh -c printenv",
    ],
)
def test_a_one_word_shell_c_payload_is_read_as_code(command: str) -> None:
    assert _check(command) == "env-dump"


@pytest.mark.parametrize(
    "command", ["bash -c ls", "bash -c 'echo hi'", "sh -c true", "python3 -c env", "bash script.sh env", "bash -l env"]
)
def test_other_shell_words_are_not_code(command: str) -> None:
    assert _check(command) is None


def test_the_force_push_regex_matches_main_in_any_case() -> None:
    rx = re.compile(dict(hardline.HARDLINE_PATTERNS)["git-push-force-main"])
    assert rx.search("git push --force origin Main")
    assert rx.search("git push origin --force MASTER")
    assert rx.search("git push --force origin HEAD:refs/heads/Main")
    assert not rx.search("git push --force origin fix/Main")
    assert not rx.search("git push --force origin Main-docs")


# ---- the quadratic paths are capped or read once ----


def test_an_endless_wrapper_and_reserved_word_alternation_is_too_deep() -> None:
    cap = hardline._ARGV_MAX_ROUNDS
    assert _check("time { " * (cap + 5) + "rm -rf ~") == "scan-depth"
    assert _check("sudo busybox " * (cap + 5) + "env") == "scan-depth"
    assert _check("time { " * (cap + 5) + "ls") == "scan-depth"
    assert hardline.parse("time { " * (cap + 5) + "ls").too_deep
    assert _check("time { " * 3 + "rm -rf ~") == "rm-rf-home"
    assert _check("time { sudo busybox env") == "env-dump"


def test_too_many_cd_directories_deny_a_relative_delete_only() -> None:
    cds = "; ".join(f"cd /srv/p{k}" for k in range(hardline._CD_MAX_DIRS + 5))
    assert _check(cds + "; rm -rf build") == "scan-depth"
    assert _check(cds + "; ls") is None
    assert _check(cds + "; rm -rf /tmp/x") is None
    few = "; ".join(f"cd /srv/p{k}" for k in range(5))
    assert _check(few + "; rm -rf build") is None


def test_each_find_action_runs_up_to_its_terminator() -> None:
    assert _check("find ~ -exec echo {} \\; -exec rm -rf {} +") == "rm-rf-home"
    assert _check("find ~ -name x -exec rm {} +") == "rm-rf-home"
    # `-exec echo -exec rm {} ;` runs `echo -exec rm FILE`: the second -exec is a word of echo
    assert _check("find ~ -exec echo -exec rm {} \\;") is None


def test_xargs_rm_fed_by_any_earlier_printed_word() -> None:
    assert _check("echo a ~ | xargs rm -rf") == "rm-rf-home"
    assert _check("echo ~ | xargs ls | xargs rm -rf") == "rm-rf-home"
    assert _check("echo ~ | xargs rm -f") is None  # not recursive
    assert _check("echo hi | xargs rm -rf") is None


# ---- final review round ----


@pytest.mark.parametrize(
    "command",
    [
        "bash -euo pipefail -c env",  # a cluster ending in `o` takes the next word as its value
        "bash -eo pipefail -c printenv",
        "bash +o posix -c env",
        "fish --command env",
        "fish --command=env",
        "fish --comm env",
    ],
)
def test_a_c_payload_after_value_options_is_read_as_code(command: str) -> None:
    assert _check(command) == "env-dump"


@pytest.mark.parametrize("command", ["sh -- -c env", "bash -c", "fish --command", "bash -o env -c ls"])
def test_a_word_that_is_not_the_c_payload_is_not_code(command: str) -> None:
    assert _check(command) is None


def test_wrapper_and_eval_chains_read_the_same_command() -> None:
    wrapped = ["sudo", "nice", "timeout", "5", "env", "X=1", "rm", "-rf", "/"]
    assert hardline.normalize_argv(wrapped) == ["rm", "-rf", "/"]
    assert hardline.normalize_argv(["eval", "eval", "rm -rf", "/"]) == ["rm", "-rf", "/"]
    assert hardline.normalize_argv(["eval", "", "rm", "x"]) == ["rm", "x"]
    assert hardline.normalize_argv(["eval", "eval", "'"]) == ["'"]
    assert hardline.normalize_argv(["env", "-S", "sudo rm", "-rf", "/"]) == ["rm", "-rf", "/"]
    assert hardline.normalize_argv(["eval", "eval"]) == ["eval"]
    assert _check("sudo " * 50 + "rm -rf ~") == "rm-rf-home"


# ---- after the final review: cd model completions ----


def test_another_users_home_is_expanded_through_the_user_database() -> None:
    root = hardline._user_home("root")
    assert root
    assert _check("rm -rf ~root") == "rm-rf-root"
    assert _check("cd ~root; rm -rf *") == "rm-rf-root"
    assert _check("rm -rf ~root/tmp/x") is None
    assert _check("rm -rf ~k3-no-such-user-x") is None  # unknown: the word stays as written


def test_a_failed_lookup_still_knows_where_root_lives(monkeypatch: pytest.MonkeyPatch) -> None:
    import pwd

    def missing(name: str) -> None:
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", missing)
    hardline._user_home.cache_clear()
    try:
        assert hardline._user_home("root") == "/root"
        assert hardline._user_home("ada") is None
        assert _check("rm -rf ~root") == "rm-rf-root"
    finally:
        hardline._user_home.cache_clear()


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("CDPATH=/ cd usr; rm -rf *", "rm-rf-root"),
        ("export CDPATH=/; cd usr; rm -rf *", "rm-rf-root"),
        ("CDPATH=$X cd usr; rm -rf *", "rm-rf-root"),  # an entry it cannot read may be /
        ("export CDPATH=/srv:/home; cd dev; rm -rf *", "rm-rf-home"),  # /home/dev is home
        ("export CDPATH='/srv:/home'; cd dev && find . -delete", "rm-rf-home"),
        ("export CDPATH=/srv; cd app; rm -rf *", None),
        ("CDPATH=/ cd ./usr; rm -rf *", None),  # bash reads no CDPATH for ./ and ../
        ("CDPATH=/ cd /srv/usr; rm -rf *", None),
    ],
)
def test_a_relative_cd_may_go_through_cdpath(command: str, rule: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDPATH", raising=False)
    assert _check(command) == rule


def test_a_cdpath_in_the_environment_counts_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDPATH", raising=False)
    assert _check("cd usr; rm -rf *") is None
    monkeypatch.setenv("CDPATH", "/")
    assert _check("cd usr; rm -rf *") == "rm-rf-root"
