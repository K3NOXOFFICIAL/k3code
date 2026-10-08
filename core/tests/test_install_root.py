"""The TUI entry and the vendor check are run from k3code's own install or source tree, never from the cwd: a cloned
repo that ships tui/dist/entry.js or scripts/vendor_check.py must not get its code executed by `k3code` / `k3code
doctor`."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code import cli as cli_mod
from k3code import doctor, paths


def _hostile_repo(root: Path) -> Path:
    for rel in ("tui/dist/entry.js", "tui/package.json", "scripts/vendor_check.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("// planted\n")
    return root


def test_a_cloned_repo_in_the_cwd_is_never_the_repo_root(tmp_path, monkeypatch):
    repo = _hostile_repo(tmp_path / "clone")
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("K3CODE_ROOT", raising=False)
    monkeypatch.chdir(repo)
    assert cli_mod._find_repo_root() != repo
    assert doctor.find_repo_root() != repo


def test_a_venv_inside_a_foreign_repo_does_not_make_that_repo_the_root(tmp_path, monkeypatch):
    repo = _hostile_repo(tmp_path / "clone")
    module = repo / ".venv" / "lib" / "python3.12" / "site-packages" / "k3code" / "paths.py"
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("K3CODE_ROOT", raising=False)
    assert repo not in paths.install_roots(module)


def test_installed_version_dir_and_explicit_env_are_roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    ver = data / "versions" / "1.2.3"
    module = ver / "venv" / "lib" / "python3.12" / "site-packages" / "k3code" / "paths.py"
    monkeypatch.setenv("K3CODE_DATA", str(data))
    monkeypatch.delenv("K3CODE_ROOT", raising=False)
    roots = paths.install_roots(module)
    assert ver in roots and data / "current" in roots
    monkeypatch.setenv("K3CODE_ROOT", str(tmp_path / "dev"))
    assert paths.install_roots(module)[0] == tmp_path / "dev"


def test_the_package_source_tree_is_a_root():
    here = Path(paths.__file__).resolve()
    assert here.parents[3] in paths.install_roots()


@pytest.mark.parametrize("finder", [cli_mod._find_repo_root, doctor.find_repo_root])
def test_explicit_env_names_a_dev_checkout(tmp_path, monkeypatch, finder):
    repo = _hostile_repo(tmp_path / "dev")
    monkeypatch.setenv("K3CODE_ROOT", str(repo))
    assert finder() == repo
