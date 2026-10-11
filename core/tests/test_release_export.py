"""The release export: built from git, free of internal paths, and the personal-data scan catches planted values."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "release" / "build_release.sh"
EXCLUDED = ("scripts/dev", "scripts/exit", "docs/reports", "GOAL.md", "panes/k3")

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("sh") is None or not (REPO / ".git").exists(),
    reason="needs a git checkout of the repository",
)


class Export(NamedTuple):
    top: str  # the folder name inside the tarball, e.g. k3code-0.1.0
    names: list[str]  # every member of the tarball
    tree: Path  # the unpacked export
    out: Path  # the output directory with the tarball and SHA256SUMS


def _env(rules: Path) -> dict[str, str]:
    return {**os.environ, "K3_RELEASE_RULES": str(rules)}


def _scan(directory: Path, rules: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPT), "--scan", str(directory)], capture_output=True, text=True, check=False, env=_env(rules)
    )


# Invented names, assembled from parts so that this file does not match the rules built from them.
HOST = "zor" + "gon"
DOMAIN = "plugh-" + "host.test"
FIRST = "Xyz" + "zy"


@pytest.fixture(scope="module")
def rules(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The scan reads its names from files outside the repository; these invented ones test the mechanism, so no real
    name or host is written anywhere in the repository."""
    folder = tmp_path_factory.mktemp("rules")
    (folder / "personal.re").write_text(
        f"# hosts and names\n{HOST}\n{DOMAIN.replace('.', '\\.')}\n\n", encoding="utf-8"
    )
    (folder / "names.re").write_text(f"{FIRST}\n", encoding="utf-8")
    return folder


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory, rules: Path) -> Export:
    out = tmp_path_factory.mktemp("release-out")
    built = subprocess.run(
        ["sh", str(SCRIPT), "-o", str(out)], capture_output=True, text=True, cwd=REPO, check=False, env=_env(rules)
    )
    assert built.returncode == 0, built.stderr
    tarball = next(out.glob("k3code-*.tar.gz"))
    unpacked = tmp_path_factory.mktemp("release-unpacked")
    with tarfile.open(tarball) as tf:
        names = tf.getnames()
        tf.extractall(unpacked, filter="data")
    top = tarball.name.removesuffix(".tar.gz")
    return Export(top, names, unpacked / top, out)


def test_export_name_follows_the_version(export: Export) -> None:
    assert re.fullmatch(r"k3code-\d+\.\d+\.\d+", export.top)
    assert (REPO / "VERSION").read_text(encoding="utf-8").strip() == export.top.removeprefix("k3code-")


def test_export_contains_no_excluded_path(export: Export) -> None:
    for path in EXCLUDED:
        prefix = f"{export.top}/{path}"
        assert not any(n == prefix or n.startswith(prefix + "/") for n in export.names), path
    assert f"{export.top}/core/pyproject.toml" in export.names  # the export is not empty


def test_export_has_checksums_for_its_tarball(export: Export) -> None:
    sums = (export.out / "SHA256SUMS").read_text(encoding="utf-8").split()
    assert sums[-1] == f"{export.top}.tar.gz"


def test_scan_passes_on_the_export(export: Export, rules: Path) -> None:
    result = _scan(export.tree, rules)
    assert result.returncode == 0, result.stderr


# The planted values are assembled from parts, so this file does not match the scan it tests.
@pytest.mark.parametrize(
    "planted",
    [
        "/home/" + HOST + "/notes.md",
        f"ssh {HOST}@example.org",
        f"https://{DOMAIN}/mcp",
        "peer at 100." + "64.0.7",
        f"{FIRST} CachedLayer",
        "/home/" + "newperson/project",
    ],
)
def test_planted_personal_value_fails_the_scan(tmp_path: Path, rules: Path, planted: str) -> None:
    (tmp_path / "notes.txt").write_text(f"{planted}\n", encoding="utf-8")
    result = _scan(tmp_path, rules)
    assert result.returncode != 0
    assert "notes.txt" in result.stderr  # the file list is printed ...
    assert planted not in result.stdout + result.stderr  # ... and the matched text is not


def test_generic_examples_pass_the_scan(tmp_path: Path, rules: Path) -> None:
    (tmp_path / "ok.txt").write_text(
        f"/home/user/x /home/u/y TokenOutput {FIRST.lower()} 192.0.2.4\n", encoding="utf-8"
    )
    assert _scan(tmp_path, rules).returncode == 0  # names.re is case-sensitive: the lower-case word is fine


def test_without_rule_files_only_the_generic_checks_run_and_it_says_so(tmp_path: Path) -> None:
    empty = tmp_path / "no-rules"
    empty.mkdir()
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text(f"{HOST} is fine here, 100." + "64.0.7 is not\n", encoding="utf-8")
    result = _scan(tree, empty)
    assert result.returncode != 0 and "a.txt" in result.stderr  # the tailnet address still fails it
    assert "no name rules" in result.stderr
    (tree / "a.txt").write_text(f"{HOST} only\n", encoding="utf-8")
    result = _scan(tree, empty)
    assert result.returncode == 0 and "no name rules" in result.stderr


# --- the scan must fail closed -------------------------------------------------------------------------------------


def _scan_tree(tmp_path: Path, rule_bytes: bytes, content: str, **env: str) -> subprocess.CompletedProcess[str]:
    folder = tmp_path / "rules"
    folder.mkdir()
    (folder / "personal.re").write_bytes(rule_bytes)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "notes.txt").write_text(content, encoding="utf-8")
    return subprocess.run(
        ["sh", str(SCRIPT), "--scan", str(tree)],
        capture_output=True,
        text=True,
        check=False,
        env={**_env(folder), **env},
    )


@pytest.mark.parametrize("rule", ["(" + HOST, "(?i)" + HOST, HOST + "\\", HOST + ")(" + HOST + "[", "(" + FIRST])
def test_a_rule_that_is_not_a_valid_regex_stops_the_scan(tmp_path: Path, rule: str) -> None:
    """grep exits 2 on `(name`, which the scan used to swallow (`|| true`): it printed "scan passed" over a tree that
    held the name. `(?i)` is only a warning in grep and then matches the literal "?i", which is no better."""
    result = _scan_tree(tmp_path, f"# names\n{rule}\n".encode(), f"written by {HOST}\n")
    assert result.returncode != 0
    assert "scan passed" not in result.stdout
    assert "personal.re line 2" in result.stderr  # the line number, so the owner can find the rule ...
    assert HOST not in result.stdout + result.stderr  # ... and never the rule itself


def test_crlf_rule_files_still_match(tmp_path: Path) -> None:
    """A rule file saved with Windows line endings used to end every regex in a carriage return, so nothing matched
    and the scan passed."""
    result = _scan_tree(tmp_path, f"{HOST}\r\n{FIRST}\r\n".encode(), f"written by {HOST}\n")
    assert result.returncode != 0
    assert "notes.txt" in result.stderr
    assert "scan passed" not in result.stdout


def test_a_grep_that_fails_is_not_a_clean_scan(tmp_path: Path) -> None:
    """Exit status 2 from any of the scan's greps (an unreadable file, ...) must fail the scan, not read as "no
    match"."""
    real_grep = shutil.which("grep")
    assert real_grep
    stub = tmp_path / "stub"
    stub.mkdir()
    # fail only the tailnet-address pass, whose pattern holds "100"
    (stub / "grep").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do case $a in *100*) echo "grep: boom" >&2; exit 2 ;; esac; done\n'
        f'exec {real_grep} "$@"\n'
    )
    (stub / "grep").chmod(0o755)
    result = _scan_tree(tmp_path, b"", "nothing here\n", PATH=f"{stub}:{os.environ['PATH']}")
    assert result.returncode != 0
    assert "scan passed" not in result.stdout
    assert "did not run to the end" in result.stderr


def test_a_signal_stops_the_build_and_removes_its_scratch_dir(tmp_path: Path) -> None:
    """The EXIT/HUP/INT/TERM trap only cleaned up, so SIGTERM during gzip let the build run on to "wrote ..." and exit
    0. It must end the build (128+15) and remove the scratch directory."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "release").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "release" / "build_release.sh")
    (repo / "VERSION").write_text("0.0.1\n", encoding="utf-8")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(home), K3_RELEASE_RULES=str(tmp_path / "no-rules"), TMPDIR=str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "Main"], cwd=repo, env=env, check=True)
    subprocess.run([*git, "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, env=env, check=True)
    real_gzip = shutil.which("gzip")
    assert real_gzip
    stub, mark = tmp_path / "stub", tmp_path / "gzip-started"
    stub.mkdir()
    (stub / "gzip").write_text(f'#!/bin/sh\n: > "{mark}"\nsleep 2\nexec {real_gzip} "$@"\n')
    (stub / "gzip").chmod(0o755)
    env["PATH"] = f"{stub}:{env['PATH']}"
    out = tmp_path / "out"
    proc = subprocess.Popen(
        ["sh", str(repo / "scripts" / "release" / "build_release.sh"), "-o", str(out), "HEAD"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(300):
            if mark.exists():
                break
            time.sleep(0.1)
        assert mark.exists(), "the build never reached gzip"
        proc.send_signal(signal.SIGTERM)
        stdout, _stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 128 + signal.SIGTERM
    assert "wrote" not in stdout
    assert not (out / "SHA256SUMS").exists()
    assert list((tmp_path / "tmp").iterdir()) == []
