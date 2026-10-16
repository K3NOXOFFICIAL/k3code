"""A ``cd`` earlier on the command line moves the directory a later relative path is read from (K3NOX-320).

The credential check used to resolve every relative argument against the session's directory, so a reader that
followed a ``cd``/``pushd`` into the home directory (or a credential folder) was judged as if it were in the project.
Everything here uses a FAKE home in a temp dir; no real credential is read.
"""

from __future__ import annotations

import pwd
from pathlib import Path
from types import SimpleNamespace

import pytest

from k3code.permissions import decide


@pytest.fixture
def scoped_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project next to a fake home holding ssh, aws and k3code credential files; ``proj/sub`` is a plain folder."""
    home = tmp_path / "home" / "alice"
    for rel in (".ssh/id_rsa", ".ssh/id_ed25519", ".aws/credentials", ".config/k3code/env"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("fake\n")
    (home / "README.md").write_text("hi\n")
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "sub" / "file.txt").write_text("ok\n")
    (proj / "README.md").write_text("hi\n")
    (proj / "sub" / "link.txt").symlink_to(home / ".ssh" / "id_rsa")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CDPATH", raising=False)
    # `~alice` resolves to the fake home, `~nobody-here` to nothing
    real_getpwnam = pwd.getpwnam
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_dir=str(home)) if name == "alice" else real_getpwnam(name),
    )
    return proj


def _decide(cmd: str, mode: str, cwd: Path):
    return decide(mode=mode, tool="bash", args={"command": cmd}, cwd=cwd)


DENIED = [
    "cd ~ && head .ssh/id_rsa",
    "pushd ~ && head .aws/credentials",
    "cd ~/.config && head k3code/env",
    "cd / && head proc/self/environ",
    "cd $HOME; base64 .ssh/id_ed25519",
    "cd ${HOME} && tac .ssh/id_rsa",
    "cd ~alice && head .ssh/id_rsa",
    "cd && head .ssh/id_rsa",  # no argument: the home directory
    "cd ~/.ssh && head id_rsa",
    "cd ~/.s* && head id_rsa",  # the glob names the folder
    # a chain of cds
    "cd ~ && cd .ssh && head id_rsa",
    "cd ~/.config; cd k3code; head env",
    "cd ~ && pushd .ssh >/dev/null && head id_rsa",
    "cd ~ && cd .config && cd k3code && head env",
    # the cd and the read are not in the same simple command or the same level
    "(cd ~ && head .ssh/id_rsa)",
    "bash -c 'cd ~ && head .ssh/id_rsa'",
    "echo $(cd ~ && head .ssh/id_rsa)",
    "cd ~ && echo go | xargs -a .ssh/id_rsa head",
    "cd ~ && head < .ssh/id_rsa",
    # braces and a relative target through the project
    "cd {~,/tmp} && head .ssh/id_rsa",
    "cd ~/.ssh/../.ssh && head id_rsa",
    # a cd after the read: a loop comes back to the read
    "for i in 1 2; do head .ssh/id_rsa; cd ~; done",
    # a symlink in the directory cd went to
    "cd sub && head link.txt",
    # a keyword or a function in front of the cd
    "if cd ~; then head .ssh/id_rsa; fi",
    "{ cd ~; }; head .ssh/id_rsa",
    "! cd ~; head .ssh/id_rsa",
    "for i in 1; do cd ~; done; head .ssh/id_rsa",
    "while cd ~; do break; done; head .ssh/id_rsa",
    "until ! cd ~; do break; done; head .ssh/id_rsa",
    "f(){ cd ~; }; f; head .ssh/id_rsa",
    "function f { cd ~; }; f; head .ssh/id_rsa",
    # a directory the stack remembers
    "pushd -n ~; popd; head .ssh/id_rsa",
    "pushd -n ~; pushd; head .ssh/id_rsa",
    # the directory a wrapper runs the command in
    "env -C ~/.ssh head id_rsa",
    "env --chdir=~/.ssh head id_rsa",
    "env -C ~ head .ssh/id_rsa",
    "sudo -D ~/.ssh head id_rsa",
    "sudo --chdir ~/.aws head credentials",
    "git -C ~/.aws diff --no-index credentials /dev/null",
    "git -C ~ show HEAD:.ssh/id_rsa",
    # the builtin spelled through a wrapper
    "builtin cd ~ && head .ssh/id_rsa",
    "command cd ~ && head .ssh/id_rsa",
]


@pytest.mark.parametrize("mode", ["default", "auto", "yolo"])
@pytest.mark.parametrize("cmd", DENIED)
def test_a_relative_credential_path_after_a_cd_is_denied(scoped_project: Path, cmd: str, mode: str) -> None:
    dec = _decide(cmd, mode, scoped_project)
    assert dec.action == "deny", (cmd, mode, dec)
    assert dec.hardline == "sensitive-path"


UNKNOWN_DIR = [
    "cd $DIR && head .ssh/id_rsa",
    'cd "$(mktemp -d)" && head .ssh/id_rsa',
    "cd - && head .ssh/id_rsa",
    "cd ~nobody-here-xyz && head .ssh/id_rsa",
    "pushd $OTHER && head id_ed25519",
    "cd $DIR && head k3code/env",
    "cd $DIR && head environ",
    "CDPATH=~ cd .ssh && head id_rsa",
    # HOME rewritten on the line: ``~``, ``$HOME`` and a bare cd no longer mean the home directory
    "HOME=$HOME/.ssh cd && head id_rsa",
    "HOME=~/.ssh; cd; head id_rsa",
    "export HOME=~/.aws; cd; head credentials",
    # a cd that is not a literal command word, or text this check does not read
    "c=cd; $c ~ && head .ssh/id_rsa",
    "$(echo cd) ~; head .ssh/id_rsa",
    "`echo cd` ~; head .ssh/id_rsa",
    "eval \"$(printf 'cd ~')\"; head .ssh/id_rsa",
    ". ./x.sh; head .ssh/id_rsa",
    "source ./x.sh; head id_rsa",
    "alias c='cd ~'\nc\nhead .ssh/id_rsa",
    "trap 'cd ~' USR1; head .ssh/id_rsa",
    # directory-stack and variable tricks
    "DIRSTACK[1]=~/.ssh; popd; head id_rsa",
    "shopt -s cdable_vars; x=~/.ssh; cd x; head id_rsa",
]


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize("cmd", UNKNOWN_DIR)
def test_a_cd_to_an_unknown_directory_asks_a_person(scoped_project: Path, cmd: str, mode: str) -> None:
    dec = _decide(cmd, mode, scoped_project)
    assert dec.action == "ask", (cmd, mode, dec)
    assert dec.needs_human, (cmd, mode, dec)


@pytest.mark.parametrize("cmd", ["cd ~ && rm .ssh/id_rsa", "cd ~/.config && rm k3code/env"])
def test_a_delete_after_a_cd_asks_a_person(scoped_project: Path, cmd: str) -> None:
    dec = _decide(cmd, "auto", scoped_project)
    assert dec.action == "ask" and dec.needs_human, (cmd, dec)


KEPT = [
    "cd sub && cat file.txt",
    "cd sub; head file.txt",
    "cd sub && cd .. && cat README.md",
    "cd ~ && cat README.md",  # the home directory is not a credential store
    "pushd sub && cat file.txt",
    "cd $DIR && cat README.md",  # an unknown directory asks only for a name that could be a credential
    "cd sub && popd && cat README.md",
    "cd sub && ls -la",
    "git -C sub status",
    "env -C sub ls",
    "if cd sub; then cat file.txt; fi",
    "f(){ cd sub; }; f; cat file.txt",
]


@pytest.mark.parametrize("cmd", KEPT)
def test_an_ordinary_cd_keeps_the_decision_of_the_command_without_it(scoped_project: Path, cmd: str) -> None:
    dec = _decide(cmd, "auto", scoped_project)
    assert dec.action == "allow", (cmd, dec)


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize("cmd", ["cd .. && head .ssh/id_rsa", "cd ../.. ; cd alice && base64 .aws/credentials"])
def test_a_cd_up_out_of_a_project_inside_the_home_is_followed(
    scoped_project: Path, monkeypatch: pytest.MonkeyPatch, cmd: str, mode: str
) -> None:
    home = scoped_project.parent / "home" / "alice"
    project = home / "work" / "proj"
    project.mkdir(parents=True)
    # the session starts two levels below the fake home; the first cd lands in work, the second in the home itself
    cmd = cmd.replace("cd .. &&", "cd ../.. &&")
    dec = _decide(cmd, mode, project)
    assert dec.action == "deny", (cmd, mode, dec)


# K3NOX-732: the four spellings K3NOX-320 left open

DIR_OPTION_DENIED = [
    # tar and make run in the directory -C names; naming the parent and then a relative member slipped past
    "tar -C ~ -cf - .ssh/id_rsa",
    "tar -cf - -C ~ .ssh/id_rsa",
    "tar -cC ~ -f - .ssh/id_rsa",
    "tar -C~ -cf - .ssh/id_rsa",
    "tar --directory=~ -cf - .ssh/id_rsa",
    "tar --directory ~/.config -cf - k3code/env",
    "tar --dir ~/.config -cf - k3code/env",
    "gtar -C ~ -cf - .aws/credentials",
    "bsdtar -C ~ -cf - .ssh/id_ed25519",
    "make -C ~ -f .ssh/id_rsa",
    "make --directory=~/.ssh -f id_rsa",
    "make -C ~/.config -f k3code/env",
    "sudo tar -C ~ -cf - .ssh/id_rsa",
]


@pytest.mark.parametrize("mode", ["default", "auto", "yolo"])
@pytest.mark.parametrize("cmd", DIR_OPTION_DENIED)
def test_a_tar_or_make_directory_option_moves_the_directory_a_member_is_read_from(
    scoped_project: Path, cmd: str, mode: str
) -> None:
    dec = _decide(cmd, mode, scoped_project)
    assert dec.action == "deny", (cmd, mode, dec)
    assert dec.hardline == "sensitive-path"


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize(
    "cmd",
    [
        "tar -C $DIR -cf - .ssh/id_rsa",
        'tar -C "$(mktemp -d)" -cf - id_rsa',
        "tar --directory=$DIR -cf - k3code/env",
        "make -C $DIR -f id_rsa",
    ],
)
def test_a_tar_or_make_directory_that_cannot_be_worked_out_asks_a_person(
    scoped_project: Path, cmd: str, mode: str
) -> None:
    dec = _decide(cmd, mode, scoped_project)
    assert dec.action == "ask" and dec.needs_human, (cmd, mode, dec)


@pytest.mark.parametrize("cmd", ["tar -C sub -cf out.tar file.txt", "tar -tf out.tar", "make -C sub -n test"])
def test_an_ordinary_tar_or_make_directory_option_is_not_judged_harder(scoped_project: Path, cmd: str) -> None:
    dec = _decide(cmd, "auto", scoped_project)
    assert dec.action != "deny" and not dec.needs_human, (cmd, dec)


LINK_THEN_CD = [
    "ln -s ~ h && cd h && head .ssh/id_rsa",
    "ln -sf ~ h; cd h; head .ssh/id_rsa",
    "ln --symbolic ~ h && pushd h && head .aws/credentials",
    "ln -s ~ h; (cd h; head .ssh/id_rsa)",
    "bash -c 'ln -s ~ h && cd h && head .ssh/id_rsa'",
    "cp -s ~/README.md h && cd h && head .ssh/id_rsa",
    "cp --symbolic-link ~/README.md h && cd h && head .ssh/id_rsa",
    "ln -s ~ h && env -C h head .ssh/id_rsa",
]


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize("cmd", LINK_THEN_CD)
def test_a_cd_after_a_symlink_made_on_the_same_line_asks_a_person(scoped_project: Path, cmd: str, mode: str) -> None:
    dec = _decide(cmd, mode, scoped_project)
    assert dec.action == "ask" and dec.needs_human, (cmd, mode, dec)


@pytest.mark.parametrize(
    "cmd",
    [
        "ln -s file.txt l && cat l",  # no cd
        "cd sub && cat file.txt",
        "ln -s file.txt sub/l && cd sub && cat file.txt",  # nothing there that could name a credential
        "cd sub && ln -s file.txt l && cat l",
    ],
)
def test_a_symlink_alone_or_a_cd_alone_is_judged_as_before(scoped_project: Path, cmd: str) -> None:
    dec = _decide(cmd, "auto", scoped_project)
    assert dec.action == "allow", (cmd, dec)


XARGS_PLACEHOLDER = [
    "printf .ssh | xargs -I{} sh -c 'cd ~/{} && head id_rsa'",
    "echo .ssh | xargs -I{} sh -c 'cd ~/{}; head id_rsa'",
    "echo .ssh | xargs -I {} bash -c 'cd ~/{}; head id_rsa'",
    "echo .ssh | xargs -I@@ sh -c 'pushd ~/@@ && head id_rsa'",
    "echo .ssh | xargs -i sh -c 'cd ~/{} && head id_rsa'",
    "echo .ssh | xargs -i@ sh -c 'cd ~/@ && head id_rsa'",
    "echo .ssh | xargs --replace sh -c 'cd ~/{} && head id_rsa'",
    "echo .ssh | xargs --replace=@ sh -c 'cd ~/@ && head id_rsa'",
    "echo .ssh | sudo xargs -I{} sh -c 'cd ~/{} && head id_rsa'",
    "echo .ssh | xargs -n1 -I{} sh -c 'cd \"~/{}\" && head id_rsa'",
    "echo .ssh | xargs -I{} sh -c 'cd {}' ; head id_rsa",
]


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize("cmd", XARGS_PLACEHOLDER)
def test_a_cd_operand_holding_the_xargs_placeholder_asks_a_person(scoped_project: Path, cmd: str, mode: str) -> None:
    dec = _decide(cmd, mode, scoped_project)
    assert dec.action == "ask" and dec.needs_human, (cmd, mode, dec)


@pytest.mark.parametrize(
    "cmd",
    [
        "ls | xargs -I{} sh -c 'cd sub && cat file.txt'",  # the placeholder is not in the cd operand
        "echo sub | xargs -I{} sh -c 'cd sub && cat file.txt {}'",
        "echo x | xargs sh -c 'cd sub && cat file.txt'",
        "echo file.txt | xargs -I{} cat {}",
    ],
)
def test_an_xargs_that_does_not_feed_a_cd_keeps_its_decision(scoped_project: Path, cmd: str) -> None:
    dec = _decide(cmd, "auto", scoped_project)
    assert dec.action == "allow", (cmd, dec)


HOME_THEN_TILDE = [
    "HOME=~/.ssh; head ~/id_rsa",
    "HOME=~/.ssh; head $HOME/id_rsa",
    "HOME=~/.ssh; head ${HOME}/id_rsa",
    'HOME=~/.ssh; base64 "$HOME/id_rsa"',
    "export HOME=~/.aws; head ~/credentials",
    "export HOME=~/.config; head ~/k3code/env",
    "HOME=~ ; HOME=$HOME/.ssh; head ~/id_ed25519",
    "HOME=~/.ssh bash -c 'head ~/id_rsa'",
    "HOME=~/.ssh sh -c 'head $HOME/id_rsa'",
    "HOME=~/.ssh; echo go | xargs -a ~/id_rsa head",
    "HOME=~/.ssh; head < ~/id_rsa",
]


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize("cmd", HOME_THEN_TILDE)
def test_a_tilde_after_home_was_reassigned_on_the_line_asks_a_person(scoped_project: Path, cmd: str, mode: str) -> None:
    dec = _decide(cmd, mode, scoped_project)
    assert dec.action == "ask" and dec.needs_human, (cmd, mode, dec)


def test_a_tilde_credential_path_stays_denied_when_home_is_reassigned(scoped_project: Path) -> None:
    dec = _decide("HOME=/nowhere; head ~/.ssh/id_rsa", "auto", scoped_project)
    assert dec.action == "deny", dec


@pytest.mark.parametrize("cmd", ["head ~/README.md", "head $HOME/notes.txt", "HOME_DIR=/tmp/x; head ~/README.md"])
def test_a_tilde_name_that_is_no_credential_is_not_asked_about(scoped_project: Path, cmd: str) -> None:
    dec = _decide(cmd, "auto", scoped_project)
    assert dec.action == "allow", (cmd, dec)


# K3NOX-750 / K3NOX-767: the next spellings of the same four ideas (a placeholder, a link, a directory option, a HOME)

ASKED = [
    # an xargs placeholder or find's ``{}`` in a reader argument, or a cd inside a nested shell
    "echo .ssh/id_rsa | xargs -I{} head ~/{}",
    "echo id_rsa | xargs -I@ head $HOME/.ssh/@",
    "echo .ssh | xargs --r=@ sh -c 'cd ~/@ && head id_rsa'",
    "echo .ssh | xargs --re=@ sh -c 'cd ~/@ && head id_rsa'",
    "find ~ -maxdepth 1 -name .ssh -execdir sh -c 'cd {} && head id_rsa' ;",
    "find ~ -maxdepth 1 -name .ssh -exec sh -c 'cd {} && head id_rsa' ;",
    "find ~ -name .ssh -okdir sh -c 'pushd {} && head id_rsa' ;",
    "find ~ -maxdepth 1 -name .ssh -execdir head {}/id_rsa ;",
    "echo .ssh | parallel sh -c 'cd ~/{} && head id_rsa'",
    "echo .ssh | parallel sh -c 'cd ~/{.} && head id_rsa'",
    "parallel sh -c 'cd ~/{} && head id_rsa' ::: .ssh",
    # a link made on the line, crossed without any cd
    "ln -s ~ h && head h/.ssh/id_rsa",
    "ln -sf ~/.ssh k; head k/id_rsa",
    "ln -s ~ sub/h && head sub/h/.ssh/id_rsa",
    "ln -s ~ h && head ./h/.aws/credentials",
    "ln -s ~ h && head $PWD/h/.ssh/id_rsa",
    "ln -s ~ -t sub && head sub/alice/.ssh/id_rsa",
    "ln -s ~ && head alice/.ssh/id_rsa",
    "cp -s ~/README.md h && head h/.ssh/id_rsa",
    "busybox ln -s ~ h && head h/.ssh/id_rsa",
    "busybox ln -s ~ h && cd h && head .ssh/id_rsa",
    # links made by extraction or checkout, then a cd
    "tar xf a.tar && cd link && head .ssh/id_rsa",
    "tar -xf a.tar; cd link; head .ssh/id_rsa",
    "tar --extract -f a.tar && pushd link && head .ssh/id_rsa",
    "tar xzf a.tgz && (cd link && head .ssh/id_rsa)",
    "bsdtar -xf a.tar && cd link && head .ssh/id_rsa",
    "unzip a.zip && cd link && head .ssh/id_rsa",
    "git checkout topic && cd link && head .ssh/id_rsa",
    "git clone https://example.invalid/r.git r && cd r/link && head .ssh/id_rsa",
    "git -C sub pull && cd sub/link && head .ssh/id_rsa",
    # HOME moved by something other than ``HOME=``
    "printf -v HOME %s ~/.ssh; head ~/id_rsa",
    "printf -vHOME %s ~/.ssh; head ~/id_rsa",
    "read HOME <<< ~/.ssh; head ~/id_rsa",
    "read -r HOME < sub/file.txt; head ~/id_rsa",
    "for HOME in ~/.ssh; do head ~/id_rsa; done",
    "select HOME in ~/.ssh; do head ~/id_rsa; break; done",
    "mapfile HOME < sub/file.txt; head ~/id_rsa",
    "declare HOME=~/.ssh; head ~/id_rsa",
    "typeset -x HOME=~/.ssh; head ~/id_rsa",
    "HOME[0]=~/.aws; head ~/credentials",
    "HOME+=/.aws; head ~/credentials",
    "declare -n h=HOME; h=~/.aws; head ~/credentials",
    "declare -n h=HOME; printf -v h %s ~/.aws; head ~/credentials",
    # a moved HOME: bare ``~``, globs, parameter expansions
    "HOME=~/.ssh; tar -cf - ~",
    "HOME=~/.ssh; cp -r ~ sub",
    "HOME=~/.ssh; cp -r $HOME sub",
    "HOME=~/.ssh; cp -r ${HOME} sub",
    "HOME=~/.aws; head ~/cred*",
    "HOME=~/.aws; head ~/c?edentials",
    "HOME=~/.aws; head ~/[c]redentials",
    "HOME=~/.aws; head ${HOME:-x}/credentials",
    "HOME=~/.aws; head ${HOME}/credentials",
    "HOME=~/.ssh; head ~/deploy_key",  # a key may carry any name
    "HOME=/tmp/x; head ~/README.md",  # the line moved HOME, so nothing below it is known
    "HOME=/tmp/x; head $HOME/notes.txt",
    # a directory option with no value: xargs appends the input as the value
    "printf '%s\\n' ~ .aws/credentials | xargs tar -cf - -C",
    "printf '%s\\n' ~ .aws/credentials | xargs tar -cf - --directory",
    "printf '%s\\n' ~ .aws/credentials | xargs tar -cC",
    "printf '%s\\n' ~ .aws/credentials | xargs make -C",
    "printf '%s\\n' ~ .aws/credentials | xargs git -C",
    "printf '%s\\n' ~ .aws/credentials | xargs env -C",
    "printf '%s\\n' ~ .aws/credentials | xargs -n2 tar -cf - -C",
    # TAR_OPTIONS carries a -C
    "TAR_OPTIONS='-C /' tar -cf - id_rsa",
    "export TAR_OPTIONS='-C /'; tar -cf - id_rsa",
    "env TAR_OPTIONS=-C/ tar -cf - id_rsa",
    "TAR_OPTIONS=--directory=$HOME; export TAR_OPTIONS; tar -cf - .ssh/id_rsa",
]


@pytest.mark.parametrize("mode", ["default", "auto"])
@pytest.mark.parametrize("cmd", ASKED)
def test_a_line_that_may_reach_a_credential_another_way_asks_a_person(
    scoped_project: Path, cmd: str, mode: str
) -> None:
    dec = _decide(cmd, mode, scoped_project)
    assert dec.action in ("ask", "deny") and (dec.action == "deny" or dec.needs_human), (cmd, mode, dec)


DIR_OPTION_MORE_DENIED = [
    # tar: an old-style bundle gives its words to the letters that take a value, in order
    "tar cfC - ~ .ssh/id_rsa",
    "tar cCf ~ - .ssh/id_rsa",
    "tar cvCf ~ - .ssh/id_rsa",
    "tar cCvf ~ - .ssh/id_rsa",
    "tar Cc ~ -f - .ssh/id_rsa",
    "tar cfCb - ~ 20 .ssh/id_rsa",
    "tar cbCf 20 ~ - .ssh/id_rsa",
    # bsdtar has its own options: --cd, and -H/-L are flags, -s takes a value
    "bsdtar --cd ~ -cf - .ssh/id_rsa",
    "bsdtar --cd=~ -cf - .ssh/id_rsa",
    "bsdtar -H -C ~ -cf - .ssh/id_rsa",
    "bsdtar -L -C ~ -cf - .ssh/id_rsa",
    "bsdtar -cHLf - -C ~ .ssh/id_rsa",
    "bsdtar -s /a/b/ -C ~ -cf - .ssh/id_rsa",
    # busybox applets
    "busybox tar -C ~ -cf - .ssh/id_rsa",
    "busybox make -C ~/.ssh -f id_rsa",
    # make: -L is a flag, --di is --directory
    "make -L -C ~ -f .config/k3code/env -p",
    "make -C ~ -L -f .config/k3code/env -p",
    "make --di ~ -f .config/k3code/env -p",
    "make --dir=~ -f .config/k3code/env -p",
    # other programs that run in a directory they are given
    "echo .aws/credentials | cpio -o -D ~",
    "echo .aws/credentials | cpio -o --directory=~",
    "echo .aws/credentials | cpio -oD ~",
    "cmake -E chdir ~ head .ssh/id_rsa",
    "uv --directory ~ run head .ssh/id_rsa",
    "uv run --directory ~ head .ssh/id_rsa",
]


@pytest.mark.parametrize("mode", ["default", "auto", "yolo"])
@pytest.mark.parametrize("cmd", DIR_OPTION_MORE_DENIED)
def test_more_directory_options_move_the_directory_a_member_is_read_from(
    scoped_project: Path, cmd: str, mode: str
) -> None:
    dec = _decide(cmd, mode, scoped_project)
    assert dec.action == "deny", (cmd, mode, dec)
    assert dec.hardline == "sensitive-path"


STILL_ALLOWED = [
    "echo file.txt | xargs -I{} cat {}",
    "echo file.txt | xargs -I{} head sub/{}",
    "echo file.txt | xargs -I{} head ./{}",
    "ls sub | xargs -I{} head sub/{}",
    "find . -name '*.txt' -exec head {} ;",
    "find sub -name '*.txt' -exec cat {} +",
    "parallel head ::: sub/file.txt",
    "ln -s file.txt l && cat l",
    "ln -s sub l && cat l/file.txt",
    "ln -s ~/README.md l && head l",
    "tar -xf a.tar && cat README.md",
    "tar -xf a.tar",
    "tar -xf a.tar -C sub",
    "unzip a.zip && cat README.md",
    "git checkout topic && cd sub && cat file.txt",
    "git clone https://example.invalid/r.git r && cd r && ls",
    "tar cf out.tar sub",
    "tar cf out.tar -C sub file.txt",
    "tar cfC out.tar sub file.txt",
    "tar cvf out.tar sub",
    "bsdtar -cf out.tar sub",
    "bsdtar -L -cf out.tar sub",
    "busybox tar -tf out.tar",
    "busybox ls",
    "make -C build",
    "make -L -n test",
    "make --directory=sub -n test",
    "make --dry-run test",
    "cpio -idv < out.cpio",
    "cmake -E echo hi",
    "cmake --build sub",
    "uv run pytest",
    "printf '%s\\n' a | xargs tar -cf out.tar -C sub",
    "printf '%s\\n' a | xargs git status",
    "printf '%s\\n' a | xargs env true",
    "TAR_OPTIONS=--warning=none tar -tf out.tar",
    "TAR_OPTIONS= true",
    "read x < sub/file.txt; cat sub/file.txt",
    "HOME_DIR=$HOME; head ~/README.md",
    "declare -n h=PATH; head ~/README.md",
    "declare -a list; head ~/README.md",
    "head ~/README.md",
    "head $HOME/README.md",
]


@pytest.mark.parametrize("cmd", STILL_ALLOWED)
def test_the_ordinary_neighbours_of_those_lines_keep_their_decision(scoped_project: Path, cmd: str) -> None:
    dec = _decide(cmd, "auto", scoped_project)
    assert dec.action == "allow", (cmd, dec)
