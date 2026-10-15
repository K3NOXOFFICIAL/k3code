"""The builtin read-only allowlist runs a command unasked only when every option is one the engine lists as safe
(``_READONLY_OPTS``), spelled exactly. Findings K3NOX-658/659/660: list-file options, 3-letter abbreviations, values
glued to a short option and options missing from the old risky list."""

from __future__ import annotations

import pytest

from k3code.permissions import PermissionMode, decide

MODES = [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS]

#: The option takes a list file or a path, so it reads or writes names the engine never checks (K3NOX-658).
LIST_FILE_OPTIONS = [
    "sort --files0-from=MARK",
    "sort --files0=MARK",
    "sort --files0-from MARK",
    "sort --files0-from=-",
    "find . -files0-from MARK",
    "file -f MARK",
    "file --files-from MARK",
    "file --files MARK",
    "wc --files0-from=MARK",
    "wc --files0=MARK",
    "du --files0-from=MARK",
    "du --files0=MARK",
    "sha256sum -c MARK",
    "sha256sum --check MARK",
    "sha256sum --chec MARK",
    "sha1sum -c MARK",
    "sha512sum -c MARK",
    "md5sum -c MARK",
]

#: A 3-letter long-option abbreviation or a value glued to a short option (K3NOX-659).
ABBREVIATED_OR_GLUED = [
    "sort --o=MARK names.txt",
    "sort --o MARK names.txt",
    "sort --out=MARK names.txt",
    "tail --f MARK",
    "tail --fol MARK",
    "date --s=10:00",
    "date --se=10:00",
    "date -f/MARK",
    "date -f/etc/hostname",
    "file -m/MARK x",
    "sort -T/MARK",
    "git show -O/MARK",
    "git diff -O/MARK",
    "git ls-files -X/MARK",
]

#: Options that write a file or run a program, missing from the old risky list (K3NOX-660), and the low ones.
WRITE_OR_RUN_OPTIONS = [
    "jq --run-tests MARK",
    "jq modulemeta",
    "yq -f MARK",
    "yq -f MARK config.yaml",
    "tree -R",
    "tree -R -H .",
    "file -z MARK",
    "file -Z MARK",
    "diff -l MARK MARK",
    "diff --paginate MARK MARK",
    "git describe --dirty",
    "git describe --broken",
]

#: The plain forms keep running without a prompt.
PLAIN = [
    "ls -la",
    "ls --color=auto -1",
    "cat -n MARK",
    "head -n 5 MARK",
    "head -5 MARK",
    "tail -n20 MARK",
    "wc -l MARK",
    "wc -lwc MARK",
    "grep -rn foo .",
    "grep -A3 -e foo MARK",
    "grep --include=*.py -r foo .",
    "rg -n -g *.py foo",
    "find . -name x -type f -maxdepth 2",
    "find . -mtime -3 -size +1k -print",
    "tree -L 2 -a -d",
    "file -b MARK",
    "file --mime-type MARK",
    "stat -c %s MARK",
    "du -sh .",
    "du -h -d 1 .",
    "date +%s",
    "date -Iseconds",
    "date -u",
    "sort -r -n -u -k 2 MARK",
    "sort -t: -k2,2 MARK",
    "uniq -c MARK",
    "cut -d: -f1 MARK",
    "tr -d x",
    "jq -r .name MARK",
    "jq -rc .a MARK",
    "yq -r .a MARK",
    "diff -u MARK MARK",
    "cmp -s MARK MARK",
    "sha256sum MARK",
    "sha256sum -b MARK",
    "realpath -s MARK",
    "readlink -f MARK",
    "git status -sb",
    "git diff --stat --cached",
    "git diff -U5 -- MARK",
    "git log --oneline -5 --author=bob",
    "git log -n 3 --format=%h",
    "git show --stat HEAD",
    "git blame -L1,5 MARK",
    "git ls-files -o --exclude-standard",
    "git describe --tags --abbrev=7",
    "git rev-parse --abbrev-ref HEAD",
    "git remote -v",
]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "MARK").write_text("x\n")
    return tmp_path


def _action(command: str, mode: PermissionMode, cwd) -> str:
    return decide(mode=mode, tool="bash", args={"command": command}, cwd=cwd).action


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("command", [*LIST_FILE_OPTIONS, *ABBREVIATED_OR_GLUED, *WRITE_OR_RUN_OPTIONS])
def test_unlisted_option_asks(command: str, mode: PermissionMode, project) -> None:
    assert _action(command, mode, project) in ("ask", "deny"), command


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("command", PLAIN)
def test_plain_form_still_allows(command: str, mode: PermissionMode, project) -> None:
    dec = decide(mode=mode, tool="bash", args={"command": command}, cwd=project)
    assert dec.action == "allow", (command, dec.message)
