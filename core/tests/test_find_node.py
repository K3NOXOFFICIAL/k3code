"""find_node(): K3CODE_NODE, then the installer's private Node, then a system node that is new enough (issue #94).

The nodes here are shell scripts that print a version, so no real node is needed.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from k3code import cli as cli_mod
from k3code import doctor
from k3code import paths as paths_mod

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake nodes are POSIX shell scripts")


def _fake_node(path: Path, version: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho v{version}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An empty PATH dir and an empty data dir; the tests add nodes to them."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("K3CODE_DATA", str(data))
    monkeypatch.delenv("K3CODE_NODE", raising=False)
    return bindir, data


def test_private_node_beats_an_old_node_on_path(world):
    """The bug: Ubuntu's node 18 on PATH was returned although the installer had fetched Node 22."""
    bindir, data = world
    _fake_node(bindir / "node", "18.19.1")
    private = _fake_node(data / "node" / "node-v22.23.3-linux-x64" / "bin" / "node", "22.23.3")
    assert paths_mod.find_node() == private


def test_private_node_is_preferred_over_a_new_enough_system_node(world):
    bindir, data = world
    _fake_node(bindir / "node", "22.1.0")
    private = _fake_node(data / "node" / "node-v22.23.3-linux-x64" / "bin" / "node", "22.23.3")
    assert paths_mod.find_node() == private


def test_new_enough_system_node_is_used_without_a_private_one(world):
    bindir, _ = world
    system = _fake_node(bindir / "node", "20.0.0")
    assert paths_mod.find_node() == system


def test_old_system_node_alone_is_refused_with_a_clear_message(world):
    bindir, _ = world
    _fake_node(bindir / "node", "18.19.1")
    assert paths_mod.find_node() is None
    msg = paths_mod.resolve_node().reason
    assert "18.19.1" in msg and "20" in msg and "K3CODE_NODE" in msg and "installer" in msg


def test_versions_compare_numerically_not_as_strings(world):
    """'8' > '20' as strings; node 8 must still be refused. 22.9 is older than 22.23."""
    bindir, data = world
    _fake_node(bindir / "node", "8.17.0")
    assert paths_mod.find_node() is None
    _fake_node(data / "node" / "node-v22.9.0-linux-x64" / "bin" / "node", "22.9.0")
    newest = _fake_node(data / "node" / "node-v22.23.3-linux-x64" / "bin" / "node", "22.23.3")
    assert paths_mod.find_node() == newest


def test_an_old_private_node_is_skipped_for_a_good_system_node(world):
    bindir, data = world
    system = _fake_node(bindir / "node", "20.11.0")
    _fake_node(data / "node" / "node-v18.20.0-linux-x64" / "bin" / "node", "18.20.0")
    assert paths_mod.find_node() == system


def test_no_node_at_all(world):
    assert paths_mod.find_node() is None
    assert "not found" in paths_mod.resolve_node().reason


def test_k3code_node_overrides_everything(world, monkeypatch, tmp_path):
    bindir, data = world
    _fake_node(bindir / "node", "22.1.0")
    _fake_node(data / "node" / "node-v22.23.3-linux-x64" / "bin" / "node", "22.23.3")
    mine = _fake_node(tmp_path / "mine" / "node", "21.0.0")
    monkeypatch.setenv("K3CODE_NODE", mine)
    assert paths_mod.find_node() == mine
    assert paths_mod.resolve_node().source == "K3CODE_NODE"


def test_k3code_node_that_does_not_exist_is_an_error_not_a_silent_fallback(world, monkeypatch, tmp_path):
    bindir, _ = world
    _fake_node(bindir / "node", "22.1.0")
    monkeypatch.setenv("K3CODE_NODE", str(tmp_path / "nowhere" / "node"))
    assert paths_mod.find_node() is None
    assert "K3CODE_NODE" in paths_mod.resolve_node().reason


def test_a_node_that_prints_no_version_is_not_used(world):
    bindir, _ = world
    path = bindir / "node"
    path.write_text("#!/bin/sh\necho hello\n")
    path.chmod(0o755)
    assert paths_mod.find_node() is None


def test_doctor_reports_the_node_it_uses_and_why(world, monkeypatch, tmp_path):
    bindir, data = world
    _fake_node(bindir / "node", "18.19.1")
    private = _fake_node(data / "node" / "node-v22.23.3-linux-x64" / "bin" / "node", "22.23.3")
    root = tmp_path / "root"
    (root / "tui" / "dist").mkdir(parents=True)
    (root / "tui" / "dist" / "entry.js").write_text("")
    monkeypatch.setattr(doctor, "find_repo_root", lambda: root)
    chk = doctor.check_tui()
    assert chk.status == doctor.OK
    assert "v22.23.3" in chk.detail and "private" in chk.detail and private in chk.detail
    assert chk.data["node"] == "v22.23.3" and chk.data["source"] == "private"


def test_doctor_says_why_an_old_system_node_is_not_usable(world, monkeypatch, tmp_path):
    bindir, _ = world
    _fake_node(bindir / "node", "18.19.1")
    monkeypatch.setattr(doctor, "find_repo_root", lambda: tmp_path)
    chk = doctor.check_tui()
    assert chk.status == doctor.WARN
    assert "18.19.1" in chk.detail and "K3CODE_NODE" in (chk.fix or "")


def test_launching_the_tui_with_only_an_old_node_names_the_problem(world, monkeypatch, tmp_path, capsys):
    bindir, _ = world
    _fake_node(bindir / "node", "18.19.1")
    root = tmp_path / "root"
    (root / "tui" / "dist").mkdir(parents=True)
    (root / "tui" / "dist" / "entry.js").write_text("")
    monkeypatch.setattr(cli_mod, "_find_repo_root", lambda: root)
    with pytest.raises(SystemExit) as exc:
        cli_mod._launch_tui(require=True)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "18.19.1" in err and "K3CODE_NODE" in err
