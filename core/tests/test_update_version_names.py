"""A version name names a directory under ``versions/``, and the update deletes or switches to that directory. The
names come from outside: a release's ``tag_name`` (from GitHub or from the mirror in ``update.api``), the last line
the installer prints, the ``previous`` file. A name that is not one install.sh makes (``..``, a ``/``, an absolute
path) named a directory outside ``versions/``, and the update deleted it or pointed ``current`` at it (K3NOX-300)."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from k3code import update as upd
from k3code.cli import cli

#: The spellings of a name outside ``versions/`` (``ABS`` is replaced with the absolute path of the victim dir).
HOSTILE = ["../victim", "1.0.0/../../victim", "ABS", "..", ".", "", "-rf", "1.0.0\n", "1.0.0 x", "1.0.0\x00"]


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setenv("K3CODE_DATA", str(d))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    (d / "versions").mkdir(parents=True)
    return d


@pytest.fixture
def victim(data: Path) -> Path:
    """A directory next to ``versions/`` that no update may delete or switch to."""
    d = data / "victim"
    d.mkdir()
    (d / "keep.me").write_text("x")
    return d


def _name(name: str, victim: Path) -> str:
    return str(victim) if name == "ABS" else name


def _version(data: Path, ver: str, *, tui: bool = False) -> Path:
    d = data / "versions" / ver
    d.mkdir(parents=True)
    (d / ".complete").write_text(ver + "\n")
    if tui:
        (d / upd.TUI_ENTRY).parent.mkdir(parents=True)
        (d / upd.TUI_ENTRY).write_text("")
    return d


def _active(data: Path) -> str:
    return Path(os.readlink(data / "current")).name


@pytest.mark.parametrize("name", HOSTILE)
def test_a_name_outside_versions_is_not_a_version_name(name: str, victim: Path) -> None:
    assert not upd.is_version_name(_name(name, victim))
    with pytest.raises(upd.InvalidVersionName):
        upd.version_dir(_name(name, victim))


@pytest.mark.parametrize(
    "name", ["0.1.0", "1.2.3-dev.10", "0.1.0-src.abc1234", "0.1.0-src.abc1234.dirty4294967295", "9.9.9-src.tree0a1b2c"]
)
def test_the_names_install_sh_makes_are_version_names(name: str, data: Path) -> None:
    assert upd.is_version_name(name)
    assert upd.version_dir(name) == data / "versions" / name


@pytest.mark.parametrize("name", HOSTILE)
def test_activate_never_deletes_or_switches_outside_versions(name: str, data: Path, victim: Path) -> None:
    """``activate('../victim')`` removed ``<data>/victim`` when its smoke test failed (reproduced on Main)."""
    _version(data, "1.0.0")
    upd.switch_to("1.0.0")
    for smoke_ok in (False, True):
        with pytest.raises(upd.InvalidVersionName):
            upd.activate(
                _name(name, victim),
                daemon_installed=lambda: False,
                smoke=lambda _p, ok=smoke_ok: (ok, "stub"),
                carry=lambda _a, _b: None,
            )
        assert (victim / "keep.me").is_file() and _active(data) == "1.0.0"


@pytest.mark.parametrize("name", HOSTILE)
def test_switch_to_never_points_current_outside_versions(name: str, data: Path, victim: Path) -> None:
    _version(data, "1.0.0")
    upd.switch_to("1.0.0")
    with pytest.raises(upd.InvalidVersionName):
        upd.switch_to(_name(name, victim))
    assert _active(data) == "1.0.0"


@pytest.mark.parametrize("name", HOSTILE)
def test_vet_staged_never_deletes_outside_versions(name: str, data: Path, victim: Path) -> None:
    """The staged version lacks the TUI the active one has: vet_staged removes it, and removed ``<data>/victim``."""
    _version(data, "1.0.0", tui=True)
    upd.switch_to("1.0.0")
    with pytest.raises(upd.InvalidVersionName):
        upd.vet_staged(_name(name, victim))
    assert (victim / "keep.me").is_file()


@pytest.mark.parametrize("name", HOSTILE)
def test_install_release_never_deletes_outside_versions(name: str, data: Path, victim: Path) -> None:
    """An incomplete ``versions/<ver>`` is removed before the download, so before any checksum is checked."""
    _version(data, "1.0.0")
    upd.switch_to("1.0.0")
    rel = upd.Release(tag=f"v{name}", version=_name(name, victim), body="", prerelease=False, assets={})
    with pytest.raises(upd.InvalidVersionName):
        upd.install_release(rel, None, allow_unpinned=True)
    assert (victim / "keep.me").is_file()


def test_rollback_never_switches_to_a_previous_outside_versions(data: Path, victim: Path) -> None:
    _version(data, "1.0.0")
    upd.switch_to("1.0.0")
    (data / "previous").write_text("../victim\n")
    assert upd.previous_version() is None
    res = upd.rollback(daemon_installed=lambda: False)
    assert not res.ok and "no previous version" in res.message
    assert _active(data) == "1.0.0"


def _releases(monkeypatch: pytest.MonkeyPatch, *tags: str) -> None:
    rels = [{"tag_name": t, "draft": False, "prerelease": False, "body": "", "assets": []} for t in tags]
    req = httpx.Request("GET", "https://mirror.invalid/")
    monkeypatch.setattr(upd.httpx, "get", lambda url, **k: httpx.Response(200, json=rels, request=req))


def test_a_release_whose_tag_is_not_a_version_name_is_not_a_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag that sorted as newest (``v1.0.0/../../x`` sorts above 1.0.0) won, and named a dir outside versions/."""
    _releases(monkeypatch, "v1.2.0", "v1.0.0/../../victim", "v/abs/path", "v9.9.9/x")
    rel = upd.fetch_latest(repo="o/r")
    assert rel is not None and rel.version == "1.2.0"
    _releases(monkeypatch, "v1.0.0/../../victim")
    assert upd.fetch_latest(repo="o/r") is None


def test_update_from_a_hostile_release_mirror_deletes_nothing(
    data: Path, victim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: `k3code update --yes` against a mirror whose only release is tagged with a path."""
    _version(data, "1.0.0")
    upd.switch_to("1.0.0")
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "stale_daemon_build", lambda: None)
    _releases(monkeypatch, "v1.0.0/../../victim")
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code != 0 and "Traceback" not in r.output
    assert (victim / "keep.me").is_file() and _active(data) == "1.0.0"


@pytest.mark.parametrize("last", ["../victim", "/abs/path", "", "k3code is installed in /x"])
def test_an_installer_that_prints_no_version_name_stages_nothing(last: str, tmp_path: Path) -> None:
    """The version name is the installer's last stdout line; anything else is an installer error, never a path."""
    installer = tmp_path / "install" / "install.sh"
    installer.parent.mkdir()
    installer.write_text(f"#!/bin/sh\nprintf '%s\\n' '{last}'\n")
    with pytest.raises(upd.SourceUpdateError, match="version name"):
        upd.update_from_source(tmp_path, pull=False, progress=lambda _line: None)
