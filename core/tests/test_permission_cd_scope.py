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
