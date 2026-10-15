"""k3code's own install is never changed by the edit tools without a person (K3NOX-742).

The installed code (the virtualenv under ``<data>/versions/<ver>``), the ``k3code``/``k3`` links in ``PREFIX/bin`` that
lead to it and the systemd user unit that starts the daemon run on the next start, with no permission engine and no
sandbox. ``write`` and ``edit`` run in the daemon, outside the sandbox, and an in-roots write is allowed in auto mode (a
session rooted at ``$HOME`` covers all of it) and in yolo mode, so the model could rewrite the permission engine itself
or the unit's start command. Writing any of them now asks in every mode, like ``.k3code/config*``, found from the
running install and the ``K3CODE_DATA`` setting rather than by name. Other files in ``~/.local/bin`` and the rest of
``$HOME`` keep their decisions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from k3code.permissions import Rule, decide, engine

MODES = ["default", "accept-edits", "auto", "yolo"]
VENV_FILES = [
    "venv/lib/python3.12/site-packages/k3code/permissions/engine.py",
    "venv/bin/k3code",
    "venv/pyvenv.cfg",
]
UNIT_FILES = ["k3code.service", "k3code-recover.service", "k3code.service.d/override.conf"]


def _home() -> Path:
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    return home


def _symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):  # pragma: no cover - no symlinks here
        pytest.skip("symlinks are not available")


def _install(data: Path, prefix: Path | None = None) -> Path:
    """An install as install.sh lays it out: ``<data>/versions/1/venv`` with ``current`` pointing at it and the two
    links in ``<prefix>/bin``. Returns the bin directory."""
    version = data / "versions" / "1"
    for rel in VENV_FILES:
        (version / rel).parent.mkdir(parents=True, exist_ok=True)
        (version / rel).write_text("x\n")
    _symlink(data / "current", version)
    bindir = (prefix or data.parent.parent) / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("k3code", "k3"):
        _symlink(bindir / name, data / "current" / "venv" / "bin" / "k3code")
    return bindir


@pytest.fixture
def layout(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """The default layout in the fake home: ``~/.local/share/k3code`` and ``~/.local/bin``. K3CODE_DATA is unset."""
    monkeypatch.delenv("K3CODE_DATA", raising=False)
    monkeypatch.delenv("K3_BIN_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    data = _home() / ".local" / "share" / "k3code"
    return data, _install(data)


def _own_files(data: Path, bindir: Path) -> list[Path]:
    unit = _home() / ".config" / "systemd" / "user"
    return [
        *(data / "current" / rel for rel in VENV_FILES),
        *(data / "versions" / "1" / rel for rel in VENV_FILES),
        data / "versions" / "2" / "venv" / "new.py",  # a version that is not there yet
        bindir / "k3code",
        bindir / "k3",
        *(unit / rel for rel in UNIT_FILES),
    ]


def _asks(mode: str, tool: str, path: str | Path, cwd: Path, **kw: object) -> bool:
    dec = decide(mode=mode, tool=tool, args={"path": str(path)}, cwd=cwd, **kw)  # type: ignore[arg-type]
    return dec.action == "ask" and dec.needs_human and dec.always_ask and not dec.auto_allowed


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_a_session_rooted_at_home_asks_for_the_install_in_every_mode(layout: tuple[Path, Path], tool: str) -> None:
    for path in _own_files(*layout):
        for mode in MODES:
            assert _asks(mode, tool, path, _home()), (mode, path)
            assert _asks(mode, tool, os.path.relpath(path, _home()), _home()), (mode, path)
        headless = decide(mode="yolo", tool=tool, args={"path": str(path)}, cwd=_home(), headless=True)
        assert headless.action == "deny", path
        assert decide(mode="plan", tool=tool, args={"path": str(path)}, cwd=_home()).action == "deny"
        assert engine.protected_path(str(path)), path


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_a_repository_session_never_gets_an_allow_for_the_install(layout: tuple[Path, Path], tool: str) -> None:
    proj = _home() / "src" / "proj"
    proj.mkdir(parents=True)
    for path in _own_files(*layout):
        for mode in MODES:
            dec = decide(mode=mode, tool=tool, args={"path": str(path)}, cwd=proj)
            assert dec.action != "allow", (mode, path, dec)
        assert decide(mode="yolo", tool=tool, args={"path": str(path)}, cwd=proj).always_ask, path


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_no_allow_rule_turns_the_ask_back_into_an_allow(layout: tuple[Path, Path], tool: str) -> None:
    data, bindir = layout
    for path in (data / "current" / VENV_FILES[0], bindir / "k3code"):
        for pattern in ("*", f"{_home()}/*", str(path)):
            rule = Rule("edit", pattern, "allow")
            for layer in ("user_rules", "project_rules", "session_rules"):
                for mode in ("auto", "yolo"):
                    assert _asks(mode, tool, path, _home(), **{layer: [rule]}), (mode, layer, pattern)
    deny = Rule("edit", f"{data}/*", "deny")
    path = data / "current" / "venv" / "pyvenv.cfg"
    assert decide(mode="yolo", tool=tool, args={"path": str(path)}, cwd=_home(), user_rules=[deny]).action == "deny"


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_a_symlink_does_not_hide_the_install(layout: tuple[Path, Path], tool: str) -> None:
    data, bindir = layout
    _symlink(_home() / "work" / "launcher", bindir / "k3code")  # a link to the bin link
    _symlink(_home() / "work" / "venv", data / "current" / "venv")  # a link to the directory
    _symlink(_home() / "work" / "lib", data / "versions" / "1" / "venv" / "lib")  # `..` after it is the venv
    _symlink(_home() / "work" / "unit.service", _home() / ".config" / "systemd" / "user" / "k3code.service")
    for mode in MODES:
        for rel in (
            "work/launcher",
            "work/venv/bin/k3code",
            "work/venv/new.py",
            "work/lib/../new.py",
            "work/unit.service",
        ):
            assert _asks(mode, tool, _home() / rel, _home()), (mode, rel)
        # the bin link written through its target, and the target written through the link
        assert _asks(mode, tool, os.path.realpath(bindir / "k3code"), _home()), mode


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_other_files_keep_their_decision(layout: tuple[Path, Path], tool: str) -> None:
    data, bindir = layout
    (_home() / "src" / "proj").mkdir(parents=True)
    other = [
        _home() / "notes.txt",
        _home() / "src" / "proj" / "app.py",
        bindir / "mytool",  # not a k3code link: PATH shadowing is not part of this guard
        _home() / ".local" / "share" / "other" / "x",
        _home() / ".config" / "other" / "x",
        data.parent / "k3code-notes" / "x",  # a sibling that only shares the prefix of the name
    ]
    for path in other:
        assert not engine.protected_path(str(path)), path
        assert decide(mode="auto", tool=tool, args={"path": str(path)}, cwd=_home()).action == "allow", path
        assert decide(mode="yolo", tool=tool, args={"path": str(path)}, cwd=_home()).action == "allow", path
        assert decide(mode="default", tool=tool, args={"path": str(path)}, cwd=_home()).action == "ask", path
        assert not decide(mode="default", tool=tool, args={"path": str(path)}, cwd=_home()).always_ask, path
    proj = _home() / "src" / "proj"
    assert decide(mode="auto", tool=tool, args={"path": "app.py"}, cwd=proj).action == "allow"


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_a_custom_data_dir_and_bin_dir_are_protected(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    data = _home() / "opt" / "k3-data"
    monkeypatch.setenv("K3CODE_DATA", str(data))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    bindir = _install(data, prefix=_home() / "opt" / "k3-prefix")
    monkeypatch.setenv("K3_BIN_DIR", str(bindir))
    monkeypatch.setattr(sys, "prefix", str(data / "current" / "venv"))
    for path in (*_own_files(data, bindir), data / "node" / "bin" / "node"):
        for mode in MODES:
            assert _asks(mode, tool, path, _home()), (mode, path)


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_the_running_install_is_found_without_k3code_data(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    """The process that decides need not have the K3CODE_DATA the install was made with: its own virtualenv says
    where the install is, and the links of a custom ``--prefix`` sit in ``<prefix>/bin``."""
    monkeypatch.delenv("K3CODE_DATA", raising=False)
    monkeypatch.delenv("K3_BIN_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    prefix = _home() / "custom"
    data = prefix / "share" / "k3code"
    bindir = _install(data)
    for running in (data / "current" / "venv", data / "versions" / "1" / "venv"):
        monkeypatch.setattr(sys, "prefix", str(running))
        for path in _own_files(data, bindir)[:-3]:
            for mode in MODES:
                assert _asks(mode, tool, path, _home()), (mode, path, running)


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_the_unit_directory_follows_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    monkeypatch.delenv("K3CODE_DATA", raising=False)
    xdg = _home() / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    for path in (xdg / "systemd" / "user" / "k3code.service", xdg / "systemd" / "user" / "k3code.service.d" / "x.conf"):
        for mode in MODES:
            assert _asks(mode, tool, path, _home()), (mode, path)
    assert decide(mode="auto", tool=tool, args={"path": str(xdg / "app.conf")}, cwd=_home()).action == "allow"
