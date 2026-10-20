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
    assert hardline._user_home("root") == "/root"
    assert hardline._user_home("ada") is None
    assert _check("rm -rf ~root") == "rm-rf-root"


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


@pytest.mark.parametrize(
    "command",
    [
        'cd "$(mktemp -d)" && rm -rf *',
        'cd "$X" && rm -rf *',
        'cd "$(mktemp -d)" && rm -rf ./*',
        'cd "${TMPDIR:-/tmp}/x" && rm -rf *',
        'cd "`mktemp -d`" && rm -rf *',
        'cd "$(printf \'%s\' "$d")" && rm -rf *',
    ],
)
def test_a_quoted_expansion_never_takes_cd_home(command: str) -> None:
    # `cd ""` fails (null directory): only an unquoted expansion that gives no word is a plain `cd`
    assert _check(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "cd $X && rm -rf *",
        "cd $(mktemp -d) && rm -rf *",
        'cd "$A"$B && rm -rf *',
        "cd `x` && rm -rf *",
        "cd ${X}; rm -rf *",
    ],
)
def test_an_unquoted_expansion_may_take_cd_home(command: str) -> None:
    assert _check(command) == "rm-rf-home"


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        ("cd / && bash -c 'rm -rf *'", DEMO),
        ("cd / && echo $(rm -rf *)", DEMO),
        ("cd / && `rm -rf *`", DEMO),
        ("cd /; sh -c 'rm -rf ./*'", DEMO),
        ("cd / && bash -c 'cd usr && bash -c \"rm -rf *\"'", DEMO),
        ("cd / && bash <<'EOF'\nrm -rf *\nEOF", DEMO),
        ("cd / && cat <<EOF\n$(rm -rf *)\nEOF", DEMO),
        ('cd / && echo "$(find . -delete)"', DEMO),
        ("cd /usr && eval 'rm -rf *'", DEMO),
        ("cd ~ && bash -c 'rm -rf *'", "/"),
        ("cd / && cd /srv/app && bash -c 'rm -rf *'", DEMO),  # / stays possible: && narrows nothing
    ],
)
def test_a_command_nested_after_a_cd_runs_where_the_cd_went(command: str, cwd: str) -> None:
    assert _check(command, cwd=cwd) is not None


@pytest.mark.parametrize(
    "command",
    [
        "cd build && bash -c 'rm -rf *'",
        "cd build && echo $(rm -rf ./*)",
        "bash -c 'cd /tmp/x && rm -rf *'",
    ],
)
def test_a_nested_command_after_a_harmless_cd_is_not_hardline(command: str) -> None:
    assert _check(command) is None


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        ("cd build && rm -rf *", HOME),
        ("cd dist && rm -rf ./*", HOME),
        ("cd x && find . -delete", HOME),
        ("cd /srv/app && rm -rf ./*", "/"),
        ("cd /srv/app && make && rm -rf ./*", "/"),
        ("if cd build && rm -rf *; then :; fi", HOME),
        ("builtin cd build && rm -rf *", HOME),
    ],
)
def test_from_home_or_root_a_delete_after_a_cd_is_refused(
    command: str, cwd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the cwd stays possible whatever joins the cd to the delete (a cd may fail, or be a function), so a session rooted
    # at $HOME or / over-denies this; from a project directory it is allowed
    monkeypatch.delenv("CDPATH", raising=False)
    assert _check(command, cwd=cwd) == ("rm-rf-home" if cwd == HOME else "rm-rf-root")
    assert _check(command) is None


@pytest.mark.parametrize(
    ("command", "cwd", "rule"),
    [
        ("cd /; cd /nonexistent; rm -rf *", "/", "rm-rf-root"),
        ("cd / && (cd tmp) && rm -rf *", "/", "rm-rf-root"),
        ("cd /; cd tmp & rm -rf *", "/", "rm-rf-root"),
        ("cd /; cd tmp | true; rm -rf *", "/", "rm-rf-root"),
        ("cd /nonexistent || rm -rf *", "/", "rm-rf-root"),
        ("cd build || true && rm -rf *", HOME, "rm-rf-home"),  # `||` ends the chain
        ("false || cd build && rm -rf *", HOME, "rm-rf-home"),  # the cd may be skipped
        ("ls | cd build && rm -rf *", HOME, "rm-rf-home"),  # a pipeline stage's cd
        ("cd build | cat && rm -rf *", HOME, "rm-rf-home"),
        ("! cd build && rm -rf *", HOME, "rm-rf-home"),
        ("cd build && true; rm -rf *", HOME, "rm-rf-home"),  # `;` ends the chain
        ("cd build && true\nrm -rf *", HOME, "rm-rf-home"),
        ("cd build && make & rm -rf *", HOME, "rm-rf-home"),
        ("cd() { :; }; cd build && rm -rf *", HOME, "rm-rf-home"),  # cd is not the builtin
        ("function cd { :; }; cd build && rm -rf *", HOME, "rm-rf-home"),
        ("alias cd=true; cd build && rm -rf *", HOME, "rm-rf-home"),
        ("enable -n cd; cd build && rm -rf *", HOME, "rm-rf-home"),
        ("\\cd build && rm -rf *", HOME, "rm-rf-home"),  # not written exactly `cd`
        ("busybox cd build && rm -rf *", HOME, "rm-rf-home"),
        ("cd $X && rm -rf *", DEMO, "rm-rf-home"),  # the target is not known
        ("cd build/* && rm -rf *", HOME, "rm-rf-home"),
        ("cd a b && rm -rf *", HOME, "rm-rf-home"),
        ("CDPATH=/srv cd build && rm -rf *", HOME, "rm-rf-home"),  # CDPATH may send it elsewhere
        ("cd /srv/app && cd $X && rm -rf *", DEMO, "rm-rf-home"),  # a later cd adds to the chain
        ("cd /srv && (cd / && rm -rf *)", DEMO, "rm-rf-root"),
        ("cd /srv && echo / | xargs rm -rf", DEMO, "rm-rf-root"),
        ("cd /srv; echo usr | xargs rm -rf", "/", "rm-rf-root"),
    ],
)
def test_a_cd_that_may_not_have_worked_keeps_the_earlier_directories(
    command: str, cwd: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CDPATH", raising=False)
    assert _check(command, cwd=cwd) == rule


def test_the_parser_records_the_subs_of_each_child() -> None:
    parsed = hardline.parse("cd / && (cd tmp) || echo $(rm x); bash -c 'ls'")
    assert parsed.subs == ["cd /", "cd tmp", "echo $(rm x)", "bash -c 'ls'"]
    assert parsed.child_subs == [[2], [3]]


@pytest.mark.parametrize(
    ("command", "cwd", "rule"),
    [
        # a cd back to an earlier directory (OLDPWD, the stack, a function, a redefined builtin) on an && chain
        ("cd /tmp && cd - && rm -rf *", "/", "rm-rf-root"),
        ("cd /tmp && cd - && find . -delete", "/", "rm-rf-root"),
        ("cd /tmp && cd - && echo * | xargs rm -rf", "/", "rm-rf-root"),
        ("cd /tmp && bash -c 'cd - && rm -rf *'", "/", "rm-rf-root"),
        ("cd /tmp && bash <<'EOF'\ncd - && rm -rf *\nEOF", "/", "rm-rf-root"),
        ("cd /; cd /tmp && cd ~- && rm -rf *", HOME, "rm-rf-home"),
        ("cd /; cd /tmp && cd ~- && rm -rf *", DEMO, "rm-rf-root"),
        ('cd /; cd /tmp && cd "$OLDPWD" && rm -rf *', DEMO, "rm-rf-root"),
        ("pushd /; cd /tmp && popd && rm -rf *", HOME, "rm-rf-home"),
        ("pushd /; cd /tmp && popd && rm -rf *", DEMO, "rm-rf-root"),
        ("f(){ cd /; }; cd /tmp && f && rm -rf *", DEMO, "rm-rf-root"),
        ("function f { cd /; }; cd /tmp && f && rm -rf *", DEMO, "rm-rf-root"),
        ("builtin() { cd /; }; builtin cd /tmp && rm -rf *", DEMO, "rm-rf-root"),
    ],
)
def test_an_and_chain_after_a_cd_still_reads_every_earlier_directory(
    command: str, cwd: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CDPATH", raising=False)
    assert _check(command, cwd=cwd) == rule


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        # one payload read once for subs in different directories: a relative cd in it starts from each of them
        ("bash -c 'cd ../..; rm -rf *'; cd /tmp; bash -c 'cd ../..; rm -rf *'", "rm-rf-home"),
        ("echo $(cd ../..; rm -rf *); cd /tmp; echo $(cd ../..; rm -rf *)", "rm-rf-home"),
        # a relative cd after a cd to an unknown place (OLDPWD, popd, a variable) starts from every directory
        ("cd /usr/lib; cd /tmp; cd -; cd ..; rm -rf *", "rm-rf-root"),
        ("cd /usr/lib; pushd /tmp; popd; cd ..; rm -rf *", "rm-rf-root"),
        ("cd /usr/lib; cd ~-; cd ..; rm -rf *", "rm-rf-root"),
        ("CDPATH=/usr/lib cd x; cd ..; rm -rf *", "rm-rf-root"),  # here is unknown after a CDPATH cd
    ],
)
def test_a_relative_cd_from_an_unknown_directory_starts_from_every_possible_one(
    command: str, rule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CDPATH", raising=False)
    assert _check(command, cwd=f"{HOME}/proj/x") == rule


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'cd build; rm -rf *'; cd /tmp; bash -c 'cd build; rm -rf *'",
        "cd /tmp; cd -; cd build; rm -rf *",
        "cd $X; cd build; rm -rf ./out",
    ],
)
def test_a_relative_cd_from_an_unknown_directory_into_a_harmless_one_is_not_hardline(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CDPATH", raising=False)
    assert _check(command, cwd=f"{HOME}/proj/x") is None


def test_past_the_directory_cap_a_cdpath_cd_walks_no_more_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDPATH", raising=False)
    chain = "; ".join(f"CDPATH=/a{k} cd b{k % 5}" for k in range(40))
    assert _check(chain + "; rm -rf build") == "scan-depth"  # too many places to read a relative target in
    assert _check(chain + "; rm -rf /") == "rm-rf-root"
    assert _check(chain + "; rm -rf /tmp/x") is None
    assert _check("CDPATH=" + ":".join(f"/a{k}" for k in range(40)) + " cd b; rm -rf build") == "scan-depth"


@pytest.mark.parametrize(
    ("command", "cwd", "rule"),
    [
        ("rm -rf ~+", HOME, "rm-rf-home"),
        ("cd / && rm -rf ~+/*", DEMO, "rm-rf-root"),
        ("cd /; cd /tmp; rm -rf ~-/*", DEMO, "rm-rf-root"),
        ("rm -rf ~-", HOME, "rm-rf-home"),
        ("rm -rf ~+/build", DEMO, None),
        ("cd /tmp; rm -rf ~-/build", DEMO, None),
    ],
)
def test_tilde_plus_and_minus_are_read_in_every_directory_of_the_line(command: str, cwd: str, rule: str | None) -> None:
    assert _check(command, cwd=cwd) == rule


# eval re-parses the words it is given: the `$X` of `eval 'cd $X'` is UNQUOTED in the cd that runs (unset, a plain `cd`:
# home), while `eval 'cd "$X"'` runs a quoted one (`cd ""` fails). The quoting was read from the outer text, where the
# single quotes hid the `$`, so a delete in $HOME after such a cd passed.
EVAL_CD_DENIED = [
    "eval 'cd $X' && rm -rf *",
    'eval "cd $X" && find . -delete',
    "eval cd \\$X && rm -rf *",
    "eval 'cd ${X}' && rm -rf *",
    "eval 'cd $(true)' && rm -rf *",
    "eval 'cd `true`' && rm -rf *",
    "eval 'cd $1' && rm -rf ./*",
    "eval 'cd $OLDPWD'; rm -rf *",
    "eval 'cd $X' && echo * | xargs rm -rf",
    "time eval 'cd $X' && rm -rf *",
]


@pytest.mark.parametrize("command", EVAL_CD_DENIED)
def test_an_unquoted_expansion_inside_eval_is_a_cd_to_home(command: str) -> None:
    assert _check(command) == "rm-rf-home", command


@pytest.mark.parametrize(
    "command",
    ["eval 'cd \"$X\"' && rm -rf *", "eval 'cd /tmp' && rm -rf build", 'eval "cd \\"$X\\"" && rm -rf *'],
)
def test_a_quoted_expansion_inside_eval_still_stays_out_of_home(command: str) -> None:
    assert _check(command) is None, command


# ~N, ~+N, ~-N are entries of the directory stack (pushd/popd/dirs): one of the directories the line may be in.
@pytest.mark.parametrize(
    "command",
    [
        "cd ~ && pushd /tmp && rm -rf ~1",
        "cd ~ && pushd /tmp && rm -rf ~+1/..",
        "pushd /tmp; rm -rf ~1/..",
        "cd ~ && pushd /tmp && find ~1 -delete",
        "cd / ; pushd /tmp; rm -rf ~-1/*",
    ],
)
def test_the_directory_stack_tilde_forms_are_read_in_every_directory_of_the_line(command: str) -> None:
    assert _check(command) in ("rm-rf-home", "rm-rf-root"), command


def test_a_directory_stack_tilde_form_in_a_project_is_not_a_home_delete() -> None:
    assert _check("rm -rf ~1/build") is None  # no pushd on the line: nothing of home or root in the set
