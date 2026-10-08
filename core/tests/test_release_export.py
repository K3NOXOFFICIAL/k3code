"""The release export: built from git, free of internal paths, and the personal-data scan catches planted values."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
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
