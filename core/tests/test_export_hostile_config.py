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


def _setup_603(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    k3home = tmp_path / "home" / ".k3code"
    k3home.mkdir(parents=True)
    (k3home / "config.yaml").write_text("model: x\n")
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    proj = tmp_path / "proj"
    (proj / ".k3code").mkdir(parents=True)
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(f"note: {MARK}\n")
    return k3home, proj, outside


def _relink_603(link: Path, target: Path) -> None:
    link.unlink()
    link.symlink_to(target)


def _exported_mark_603(tmp_path: Path, proj: Path) -> bool:
    out = tmp_path / "x.k3bundle"
    write_bundle(out, store=SessionStore(tmp_path / "s.db"), cwd=proj, session_ids=[], settings=True)
    with tarfile.open(out) as tar:
        return MARK.encode() in tar.extractfile("settings/project.config.yaml").read()


def test_containment_holds_when_a_resolved_name_turns_into_a_link_before_the_stat(tmp_path: Path, monkeypatch) -> None:
    """open lands on the outside file through proj/inner.yaml; the name is a regular file while it is resolved and a
    link out again when the resolved name is stat'ed."""
    _, proj, outside = _setup_603(tmp_path, monkeypatch)
    keep = proj / "keep.yaml"
    keep.write_text("note: inside\n")
    inner = proj / "inner.yaml"
    inner.symlink_to(outside)
    (proj / ".k3code" / "config.yaml").symlink_to(inner)
    real_open, real_stat = os.open, os.stat
    # The name a check by name resolves to while inner is the in-project file. Taken now, before os.stat is patched:
    # Path.resolve() stats, so resolving inside the patched stat would call it again without end.
    inner_resolved = str(proj.resolve() / "inner.yaml")
    opened: list[bool] = []

    def open_(path, *a, **kw):
        if not str(path).endswith(".k3code/config.yaml"):
            return real_open(path, *a, **kw)
        opened.append(True)
        _relink_603(inner, outside)  # every open (trust's own read too) lands outside
        fd = real_open(path, *a, **kw)
        inner.unlink()
        os.link(keep, inner)  # a regular in-project file while the name is resolved
        return fd

    def stat_(path, *a, **kw):
        if str(path) == inner_resolved and not os.path.islink(inner):  # lstat: not this patched stat
            _relink_603(inner, outside)  # and a link out again for the stat
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(trust.os, "open", open_)
    monkeypatch.setattr(trust.os, "stat", stat_)
    with pytest.raises(BundleError, match="does not parse") as e:
        _exported_mark_603(tmp_path, proj)
    assert opened and MARK not in str(e.value)
    assert not (tmp_path / "x.k3bundle").exists()


def test_a_link_to_the_user_config_while_it_is_checked_does_not_unlock_the_plain_read(
    tmp_path: Path, monkeypatch
) -> None:
    """config.yaml points at the user's config while _read asks whether it is the user's, and out of the project
    when it is read."""
    k3home, proj, outside = _setup_603(tmp_path, monkeypatch)
    cfg = proj / ".k3code" / "config.yaml"
    cfg.symlink_to(outside)
    real = trust.project_dir_of

    def project_dir_of(path):
        _relink_603(cfg, k3home / "config.yaml")
        try:
            return real(path)
        finally:
            _relink_603(cfg, outside)

    monkeypatch.setattr(trust, "project_dir_of", project_dir_of)
    try:
        assert not _exported_mark_603(tmp_path, proj)
    except BundleError as e:
        assert MARK not in str(e)


def _no_proc(monkeypatch) -> dict[int, str]:
    """A system without ``/proc`` (macOS): reading ``/proc/self/fd/N`` fails. Returns where each fd really is, for a
    stand-in ``F_GETPATH``."""
    real_readlink = os.readlink
    seen: dict[int, str] = {}

    def readlink(path, *a, **kw):
        if str(path).startswith("/proc/self/fd/"):
            seen[int(str(path).rsplit("/", 1)[1])] = real_readlink(path, *a, **kw)
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_readlink(path, *a, **kw)

    monkeypatch.setattr(trust.os, "readlink", readlink)
    return seen


class _FakeFcntl:
    """``fcntl`` as macOS has it: ``F_GETPATH`` writes the opened file's path into the buffer it is given."""

    F_GETPATH = 50

    def __init__(self, where: dict[int, str]) -> None:
        self.where = where

    def fcntl(self, fd: int, cmd: int, arg: bytes) -> bytes:
        assert cmd == self.F_GETPATH
        return os.fsencode(self.where[fd]).ljust(len(arg), b"\0")  # the failed /proc lookup recorded it


def test_without_proc_the_place_is_read_with_f_getpath(tmp_path: Path, monkeypatch) -> None:
    """macOS has no /proc: the opened file's place comes from ``F_GETPATH`` there, so a config linked inside the
    project still exports and one linked out is still refused."""
    where = _no_proc(monkeypatch)
    monkeypatch.setattr(trust, "fcntl", _FakeFcntl(where))
    proj = _project(tmp_path)
    (proj / "shared.yaml").write_text("note: inside-marker\n")
    (proj / ".k3code" / "config.yaml").symlink_to(proj / "shared.yaml")
    with tarfile.open(_export(tmp_path, proj, True)) as tar:
        assert b"inside-marker" in tar.extractfile("settings/project.config.yaml").read()
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(f"note: {MARK}\n")
    _swap_link(proj / ".k3code" / "config.yaml", outside)
    (tmp_path / "x.k3bundle").unlink()
    with pytest.raises(BundleError, match="does not parse") as e:
        _export(tmp_path, proj, True)
    assert MARK not in str(e.value)


def test_without_proc_or_f_getpath_the_read_is_refused(tmp_path: Path, monkeypatch) -> None:
    _no_proc(monkeypatch)
    monkeypatch.setattr(trust, "fcntl", None)
    proj = _project(tmp_path)
    (proj / ".k3code" / "config.yaml").write_text("note: x\n")
    with pytest.raises(OSError):
        trust.read_bounded(proj / ".k3code" / "config.yaml", within=proj)
    assert trust.read_bounded(proj / ".k3code" / "config.yaml") == b"note: x\n"  # no place to check: read
