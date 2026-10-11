"""scripts/release/rewrite_history.sh: what it refuses to delete, --main-only on a lone Main, and that its verification
sees every rule form git-filter-repo reads.

git-filter-repo is not needed: `uv` is a stub that rewrites nothing (so the history still holds every value the rules
name, which the verification must report). The script runs from a copy in a temporary "repository", so the checkout it
guards against deleting is a temporary one too, and HOME is a temporary directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RELEASE_DIR = REPO / "scripts" / "release"
# Invented, assembled from parts so this file does not hold the word it plants.
WORD = "plug" + "hword"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git"
)


class Sandbox:
    def __init__(self, tmp: Path, messages: str = f"{WORD}==>X\n") -> None:
        self.tmp = tmp
        self.fake_repo = tmp / "fakerepo"
        (self.fake_repo / "scripts" / "release").mkdir(parents=True)
        for name in ("rewrite_history.sh", "remap_hashes.py"):
            shutil.copy(RELEASE_DIR / name, self.fake_repo / "scripts" / "release" / name)
        self.script = self.fake_repo / "scripts" / "release" / "rewrite_history.sh"
        self.home = tmp / "home"
        self.home.mkdir()
        self.rules = tmp / "rules"
        self.rules.mkdir()
        (self.rules / "replacements.txt").write_text(f"{WORD}==>X\n")
        (self.rules / "messages.txt").write_bytes(messages.encode())
        (self.rules / "mailmap").write_text("Public Name <public@example.invalid> <personal@example.invalid>\n")
        bindir = tmp / "bin"
        bindir.mkdir()
        # git-filter-repo is not installed here: this stand-in only leaves the commit map the next step reads
        (bindir / "uv").write_text("#!/bin/sh\nmkdir -p filter-repo\nprintf 'old new\\n' > filter-repo/commit-map\n")
        (bindir / "gitleaks").write_text("#!/bin/sh\nexit 0\n")
        for f in bindir.iterdir():
            f.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(HOME=str(self.home), PATH=f"{bindir}:{self.env['PATH']}")
        self.source = tmp / "source"

    def git(self, *args: str, cwd: Path | None = None) -> None:
        cmd = ["git", "-c", "user.name=t", "-c", "user.email=public@example.invalid", *args]
        subprocess.run(cmd, cwd=cwd or self.source, env=self.env, check=True, capture_output=True)

    def make_source(self, *, branches: tuple[str, ...] = (), planted: bool = True) -> None:
        self.source.mkdir()
        self.git("init", "-q", "-b", "Main")
        (self.source / "a.txt").write_text(f"the word is {WORD}\n" if planted else "nothing here\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"add {WORD}" if planted else "add a file")
        for name in branches:
            self.git("branch", name)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["bash", str(self.script), "--rules", str(self.rules), *args]
        return subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=120, check=False)


# --- --work is only ever a directory the script made ---------------------------------------------------------------


def test_a_work_directory_with_files_is_not_deleted_when_the_source_is_wrong(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    work = tmp_path / "precious"
    work.mkdir()
    (work / "keep.txt").write_text("mine\n")
    r = box.run("--work", str(work), "--source", str(tmp_path / "nonexistent"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "refusing --work" in r.stderr
    assert (work / "keep.txt").read_text() == "mine\n"


def test_work_may_not_be_the_home_directory_the_checkout_or_a_parent_of_either(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    (box.home / "doc.txt").write_text("mine\n")
    for bad in (box.home, box.fake_repo, tmp_path, box.home / ".." / "home", box.fake_repo / "scripts" / ".."):
        r = box.run("--work", str(bad), "--source", str(tmp_path / "nonexistent"))
        assert r.returncode == 2, (bad, r.stdout, r.stderr)
        assert "refusing --work" in r.stderr
    assert (box.home / "doc.txt").read_text() == "mine\n"
    assert (box.fake_repo / "scripts" / "release" / "rewrite_history.sh").is_file()


def test_work_may_not_be_a_file_or_a_symlink(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("mine\n")
    link = tmp_path / "link"
    link.symlink_to(target)
    afile = tmp_path / "afile"
    afile.write_text("x\n")
    for bad in (link, afile):
        r = box.run("--work", str(bad), "--source", str(tmp_path / "nonexistent"))
        assert r.returncode == 2 and "refusing --work" in r.stderr, (bad, r.stderr)
    assert (target / "keep.txt").is_file() and afile.is_file()


def test_a_new_an_empty_and_a_previous_work_directory_are_fine(tmp_path: Path) -> None:
    box = Sandbox(tmp_path, messages="neverplanted==>X\n")
    box.make_source()
    for name in ("new", "empty", "new"):  # the second "new" is the first run's output, which carries the marker
        work = tmp_path / name
        if name == "empty":
            work.mkdir()
        r = box.run("--work", str(work), "--source", str(box.source))
        assert r.returncode == 0, (name, r.stdout, r.stderr)
        assert "verification passed" in r.stdout
        assert (work / ".k3code-rewrite-work").is_file()


# --- --main-only ------------------------------------------------------------------------------------------------


def test_main_only_works_on_a_source_that_has_nothing_but_main(tmp_path: Path) -> None:
    """`grep -v` exits 1 when no branch is left to delete, which pipefail turned into a silent rc=1."""
    box = Sandbox(tmp_path, messages="neverplanted==>X\n")
    box.make_source()
    r = box.run("--work", str(tmp_path / "w"), "--source", str(box.source), "--main-only")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "verification passed" in r.stdout


def test_main_only_drops_the_other_branches(tmp_path: Path) -> None:
    box = Sandbox(tmp_path, messages="neverplanted==>X\n")
    box.make_source(branches=("feat/a", "wip"))
    work = tmp_path / "w"
    r = box.run("--work", str(work), "--source", str(box.source), "--main-only")
    assert r.returncode == 0, r.stdout + r.stderr
    heads = subprocess.run(
        ["git", "-C", str(work / "repo.git"), "for-each-ref", "--format=%(refname)", "refs/heads"],
        capture_output=True,
        text=True,
        check=True,
        env=box.env,
    ).stdout.split()
    assert heads == ["refs/heads/Main"]


# --- the verification sees every rule form ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        f"{WORD}==>X",  # no prefix: a literal
        f"literal:{WORD}==>X",
        f"literal:{WORD}",  # no replacement
        "regex:plug[h]word==>X",
        "glob:plug*word==>X",
        f"glob:{WORD[:-1]}?==>X",
        f"x==>y==>z\n{WORD}==>X",  # an earlier rule with two arrows does not hide this one
    ],
)
def test_verification_reports_a_value_still_in_the_history(tmp_path: Path, rule: str) -> None:
    """Only `regex:` was stripped before grepping, so `literal:word` searched for the text "literal:word", found
    nothing, and the check passed over a history that still held the word."""
    box = Sandbox(tmp_path, messages=rule + "\n")
    box.make_source()
    r = box.run("--work", str(tmp_path / "w"), "--source", str(box.source))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL: a removed value is still present" in r.stdout
    assert "verification passed" not in r.stdout
    assert WORD not in r.stdout + r.stderr  # the value is never printed


@pytest.mark.parametrize(
    "rule",
    [
        "literal:neverplanted==>X",
        "regex:never[p]lanted==>X",
        "glob:never*planted==>X",
        "neverplanted==>X",
        "# not a rule? it is one to filter-repo, and it is not in the history either",
        "==>X",  # an empty pattern: filter-repo skips it
    ],
)
def test_verification_passes_when_the_history_is_clean(tmp_path: Path, rule: str) -> None:
    box = Sandbox(tmp_path, messages=rule + "\n\n")
    box.make_source(planted=False)
    r = box.run("--work", str(tmp_path / "w"), "--source", str(box.source))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "verification passed" in r.stdout


def test_verification_handles_crlf_rule_files(tmp_path: Path) -> None:
    box = Sandbox(tmp_path, messages=f"literal:{WORD}==>X\r\n")
    box.make_source()
    r = box.run("--work", str(tmp_path / "w"), "--source", str(box.source))
    assert r.returncode == 1 and "FAIL: a removed value is still present" in r.stdout


def test_a_pattern_grep_cannot_read_fails_the_verification_instead_of_passing_it(tmp_path: Path) -> None:
    box = Sandbox(tmp_path, messages="regex:(unclosed==>X\n")
    box.make_source(planted=False)
    r = box.run("--work", str(tmp_path / "w"), "--source", str(box.source))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not be checked" in r.stdout
    assert "verification passed" not in r.stdout
