"""The release export: built from git, free of internal paths, and the personal-data scan catches planted values."""

from __future__ import annotations

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


def _scan(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(SCRIPT), "--scan", str(directory)], capture_output=True, text=True, check=False)


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory) -> Export:
    out = tmp_path_factory.mktemp("release-out")
    built = subprocess.run(["sh", str(SCRIPT), "-o", str(out)], capture_output=True, text=True, cwd=REPO, check=False)
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


def test_scan_passes_on_the_export(export: Export) -> None:
    result = _scan(export.tree)
    assert result.returncode == 0, result.stderr


# The planted values are assembled from parts, so this file does not match the scan it tests.
@pytest.mark.parametrize(
    "planted",
    [
        "/home/" + "ke" + "no/notes.md",
        "ssh " + "ke" + "no@example.org",
        "https://mcp." + "k3nox" + ".com/mcp",
        "peer at 100." + "64.0.7",
        "Ni" + "ls CachedLayer",
        "/home/" + "newperson/project",
    ],
)
def test_planted_personal_value_fails_the_scan(tmp_path: Path, planted: str) -> None:
    (tmp_path / "notes.txt").write_text(f"{planted}\n", encoding="utf-8")
    result = _scan(tmp_path)
    assert result.returncode != 0
    assert "notes.txt" in result.stderr  # the file list is printed ...
    assert planted not in result.stdout + result.stderr  # ... and the matched text is not


def test_generic_examples_pass_the_scan(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("/home/user/x /home/u/y TokenOutput 192.0.2.4\n", encoding="utf-8")
    assert _scan(tmp_path).returncode == 0
