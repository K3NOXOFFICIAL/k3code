"""K3NOX-585: /export of a project whose ``.k3code/config.yaml`` is hostile (alias bomb, deep nesting, huge, a FIFO,
a symlink out of the project) ends fast in a one-line refusal or a normal export and reads nothing outside it."""

from __future__ import annotations

import contextlib
import os
import signal
import tarfile
from pathlib import Path

import pytest

from k3code import trust
from k3code.bundle import BundleError, write_bundle
from k3code.gateway.sessions import SessionStore

MARK = "outside-marker-7731"


@contextlib.contextmanager
def _within(seconds: int = 5):
    """Fail instead of hang: the hostile files above block or spin for minutes."""

    def boom(*_):
        raise AssertionError(f"still running after {seconds}s")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _alias_bomb() -> str:
    lines = ["x: &a0 [" + ", ".join(["lol"] * 9) + "]"]
    for i in range(1, 9):
        lines.append(f"x{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]")
    return "\n".join(lines) + "\n"


HOSTILE = {
    "alias-bomb": _alias_bomb,
    "deep-list": lambda: "x: " + "[" * 20000 + "\n",
    "deep-map": lambda: "x: " + "{a: " * 3000 + "\n",
    "huge": lambda: "".join(f"k{i}: v\n" for i in range(200_000)),
}


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".k3code").mkdir(parents=True)
    return proj


def _export(tmp_path: Path, proj: Path, settings: bool) -> Path:
    out = tmp_path / "x.k3bundle"
    with _within():
        write_bundle(out, store=SessionStore(tmp_path / "s.db"), cwd=proj, session_ids=[], settings=settings)
    return out


@pytest.mark.parametrize("settings", [True, False])
@pytest.mark.parametrize("kind", HOSTILE)
def test_export_refuses_a_hostile_project_config(tmp_path: Path, kind: str, settings: bool) -> None:
    proj = _project(tmp_path)
    (proj / ".k3code" / "config.yaml").write_text(HOSTILE[kind]())
    with pytest.raises(BundleError, match="does not parse") as e:
        _export(tmp_path, proj, settings)
    assert "lol" not in str(e.value)


@pytest.mark.parametrize("settings", [True, False])
def test_export_does_not_block_on_a_fifo_config(tmp_path: Path, settings: bool) -> None:
    proj = _project(tmp_path)
    os.mkfifo(proj / ".k3code" / "config.yaml")
    out = _export(tmp_path, proj, settings)
    assert out.exists()


@pytest.mark.parametrize("settings", [True, False])
def test_export_refuses_a_config_symlinked_out_of_the_project(tmp_path: Path, settings: bool) -> None:
    proj = _project(tmp_path)
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(f"note: {MARK}\nmax_tokens: 4096\n")
    (proj / ".k3code" / "config.yaml").symlink_to(outside)
    with pytest.raises(BundleError, match="does not parse") as e:
        _export(tmp_path, proj, settings)
    assert MARK not in str(e.value)
    assert not (tmp_path / "x.k3bundle").exists()


def test_export_reads_a_config_symlinked_inside_the_project(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / "shared.yaml").write_text("note: inside-marker\n")
    (proj / ".k3code" / "config.yaml").symlink_to(proj / "shared.yaml")
    out = _export(tmp_path, proj, True)
    with tarfile.open(out) as tar:
        assert b"inside-marker" in tar.extractfile("settings/project.config.yaml").read()


def test_trust_does_not_block_on_a_fifo_or_read_a_huge_config(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    cfg = proj / ".k3code" / "config.yaml"
    os.mkfifo(cfg)
    with _within():
        assert trust.trusted_text(proj) is None
    cfg.unlink()
    cfg.write_text("k: " + "v" * (trust.MAX_CONFIG_BYTES + 1) + "\n")
    with _within():
        assert trust.trusted_text(proj) is None


def _swap_link(link: Path, target: Path) -> None:
    link.unlink()
    link.symlink_to(target)


def test_export_refuses_a_link_swapped_out_of_the_project_as_it_is_opened(tmp_path: Path, monkeypatch) -> None:
    """The containment is checked on the file that was opened, not on the name a moment before: a script of the repo
    can point ``config.yaml`` outside the project just as the export opens it, and back right after."""
    proj = _project(tmp_path)
    inside, outside = proj / "shared.yaml", tmp_path / "elsewhere.yaml"
    inside.write_text("note: inside-marker\n")
    outside.write_text(f"note: {MARK}\n")
    link = proj / ".k3code" / "config.yaml"
    link.symlink_to(inside)
    real_open = os.open
    swapped: list[bool] = []

    def open_(path, *a, **kw):
        swapped.append(True)
        _swap_link(link, outside)
        try:
            return real_open(path, *a, **kw)
        finally:
            _swap_link(link, inside)

    monkeypatch.setattr(trust.os, "open", open_)
    with pytest.raises(BundleError, match="does not parse") as e:
        _export(tmp_path, proj, True)
    assert swapped and MARK not in str(e.value)
    assert not (tmp_path / "x.k3bundle").exists()


def test_export_from_home_reads_the_user_config_as_the_user_wrote_it(tmp_path: Path, monkeypatch) -> None:
    """With the default ``K3CODE_HOME=~/.k3code``, ``~/.k3code/config.yaml`` is the user's own config, not a project's:
    an anchor in it, or a link to a dotfiles repo elsewhere, is theirs to keep."""
    home = tmp_path / "home"
    (home / ".k3code").mkdir(parents=True)
    dotfiles = tmp_path / "dotfiles.yaml"
    dotfiles.write_text("base: &b {max_tokens: 4096}\nagain: *b\n")
    (home / ".k3code" / "config.yaml").symlink_to(dotfiles)
    monkeypatch.setenv("K3CODE_HOME", str(home / ".k3code"))
    out = _export(tmp_path, home, True)
    with tarfile.open(out) as tar:
        assert b"max_tokens" in tar.extractfile("settings/user.config.yaml").read()
