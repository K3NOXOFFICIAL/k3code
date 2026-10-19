"""The grep fallback decides on the file it opened, not on the path it checked a moment before (K3NOX-663, K3NOX-657).

A background job can swap a directory of the project for a link to a directory outside every root while
grep runs; a check by path name followed by an open by path name loses that race. A FIFO in the tree has no writer,
so a plain ``open`` blocks the search thread for good.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import k3code.tools as tools
from k3code.tools import grep_worker

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc (Linux)")

#: built at run time so the secret scan never sees a key-shaped literal
SECRET = "not-a-real" + "-value-k663"
AT_FDCWD = -100
RENAME_EXCHANGE = 2
RACE_RUNS = 25
RACE_FILES = 20
GIT = shutil.which("git")  # looked up before the fixture empties PATH


@pytest.fixture
def fallback_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH holds no rg (and only git when a test links it in), so grep takes the Python fallback."""
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    bins = tmp_path / "bin"
    bins.mkdir()
    monkeypatch.setenv("PATH", str(bins))
    return bins


def _track_with_git(bins: Path, proj: Path) -> None:
    if not GIT:
        pytest.skip("git not installed")
    (bins / "git").symlink_to(GIT)
    subprocess.run([GIT, "-C", str(proj), "init", "-q"], check=True)
    subprocess.run([GIT, "-C", str(proj), "add", "-A"], check=True)


def _racer(proj: str, stop: threading.Event) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    a, b = f"{proj}/cfg".encode(), f"{proj}/cfg_swap".encode()
    while not stop.is_set():
        libc.renameat2(AT_FDCWD, a, AT_FDCWD, b, RENAME_EXCHANGE)


@pytest.mark.parametrize("use_git", [False, True], ids=["walk", "git-ls-files"])
def test_a_directory_swapped_for_a_link_during_the_search_leaks_nothing(tmp_path, fallback_only, use_git) -> None:
    outside = tmp_path / "outside"
    proj = tmp_path / "proj"
    outside.mkdir()
    (proj / "cfg").mkdir(parents=True)
    for i in range(RACE_FILES):
        (outside / f"f{i}").write_text(f"TOKEN={SECRET}\n")
        (proj / "cfg" / f"f{i}").write_text("TOKEN in the repo\n")
    if use_git:
        _track_with_git(fallback_only, proj)
    (proj / "cfg_swap").symlink_to(outside)

    stop = threading.Event()
    racer = threading.Thread(target=_racer, args=(str(proj), stop))
    racer.start()
    leaked = hits = 0
    try:
        for _ in range(RACE_RUNS):
            res = tools._grep_fallback("TOKEN", proj, None, None)
            leaked += SECRET in res["matches"]
            hits += "TOKEN in the repo" in res["matches"]
    finally:
        stop.set()
        racer.join()
    assert leaked == 0
    assert hits  # the search itself still worked while the swap ran


@pytest.mark.parametrize("use_git", [False, True], ids=["walk", "git-ls-files"])
def test_a_fifo_in_the_tree_is_skipped_and_the_search_returns(tmp_path, fallback_only, use_git) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("TOKEN in a\n")
    (proj / "b.txt").write_text("tracked, then replaced by a pipe\n")
    if use_git:
        _track_with_git(fallback_only, proj)
        (proj / "b.txt").unlink()  # the index still lists it
    os.mkfifo(proj / "b.txt" if use_git else proj / "p")

    out: dict = {}
    worker = threading.Thread(target=lambda: out.update(tools._grep_fallback("TOKEN", proj, None, None)), daemon=True)
    worker.start()
    worker.join(5)
    if worker.is_alive():  # unfixed code: release the blocked open so the thread does not outlive the test
        fd = os.open(proj / ("b.txt" if use_git else "p"), os.O_RDWR | os.O_NONBLOCK)
        os.close(fd)
        worker.join(5)
        pytest.fail("the grep fallback blocked on a FIFO")
    assert "a.txt:1:TOKEN in a" in out["matches"]
    assert "partial" not in out.get("warnings", "")


def _scan(opener: grep_worker.Beneath, rel: str, display: str) -> list[str | None]:
    match, literal = grep_worker.matcher("TOKEN")
    with opener.open(rel) as f:
        return list(grep_worker.scan(f, display, match, literal, 1e18, False, {"long": 0}))


def test_a_file_swapped_for_a_link_after_the_walk_is_not_read(tmp_path, fallback_only) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text(f"TOKEN={SECRET}\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("TOKEN in a\n")
    opener = grep_worker.Beneath(os.path.realpath(proj))
    try:
        assert _scan(opener, "a.txt", str(proj / "a.txt")) == [f"{proj / 'a.txt'}:1:TOKEN in a"]
        (proj / "a.txt").unlink()
        (proj / "a.txt").symlink_to(secret)
        with pytest.raises(OSError):
            _scan(opener, "a.txt", str(proj / "a.txt"))
    finally:
        opener.close()


def test_a_walked_file_below_a_directory_that_is_a_link_is_not_read(tmp_path, fallback_only) -> None:
    """A parent directory swapped for a link: the name looks right, but the open follows no link on the way."""
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "a.txt").write_text(f"TOKEN={SECRET}\n")
    (tmp_path / "proj").symlink_to(tmp_path / "elsewhere")
    opener = grep_worker.Beneath(os.path.realpath(tmp_path))  # the walk listed proj/a.txt below it
    try:
        with pytest.raises(OSError):
            _scan(opener, "proj/a.txt", str(tmp_path / "proj" / "a.txt"))
    finally:
        opener.close()


def test_a_stale_index_entry_behind_a_link_opens_nothing_outside_the_roots(tmp_path, fallback_only) -> None:
    """Not a race: the index lists cfg/p and cfg is now a link to a directory holding a FIFO. A writer blocked on that
    FIFO is released by any open for reading, so it shows whether the fallback opened a path outside the roots."""
    outside = tmp_path / "outside"
    proj = tmp_path / "proj"
    outside.mkdir()
    (proj / "cfg").mkdir(parents=True)
    (proj / "cfg" / "p").write_text("tracked\n")
    (proj / "a.txt").write_text("TOKEN here\n")
    _track_with_git(fallback_only, proj)
    shutil.rmtree(proj / "cfg")
    (proj / "cfg").symlink_to(outside)
    os.mkfifo(outside / "p")

    opened = threading.Event()

    def writer() -> None:
        fd = os.open(outside / "p", os.O_WRONLY)  # blocks until a reader opens the FIFO
        opened.set()
        os.close(fd)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    res = tools._grep_fallback("TOKEN", proj, None, None)
    t.join(1)
    outside_was_opened = opened.is_set()
    if not outside_was_opened:  # release the writer
        os.close(os.open(outside / "p", os.O_RDONLY | os.O_NONBLOCK))
        t.join(1)
    assert "a.txt:1:TOKEN here" in res["matches"]
    assert not outside_was_opened


def test_the_fallback_opens_without_becoming_the_controlling_terminal(tmp_path, monkeypatch) -> None:
    seen: list[int] = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, flags, *a, **k: (seen.append(flags), real_open(p, flags, *a, **k))[1])
    (tmp_path / "a.txt").write_text("TOKEN\n")
    opener = grep_worker.Beneath(os.path.realpath(tmp_path))
    try:
        _scan(opener, "a.txt", str(tmp_path / "a.txt"))
    finally:
        opener.close()
    assert seen and all(f & os.O_NOCTTY and f & os.O_NONBLOCK and f & os.O_NOFOLLOW for f in seen)


def test_without_proc_the_walk_reads_the_opened_path_off_the_descriptor(tmp_path, fallback_only, monkeypatch) -> None:
    """macOS has no /proc: the walk still confirms each open file (F_GETPATH there) instead of skipping them all."""
    import fcntl

    real_readlink, real_fcntl, getpath = os.readlink, fcntl.fcntl, 50  # 50: F_GETPATH on macOS

    def no_proc(path, *a, **k):
        if str(path).startswith("/proc/self/fd/"):
            raise FileNotFoundError(path)
        return real_readlink(path, *a, **k)

    def fake_fcntl(fd, op, *a):
        if op == getpath:
            return real_readlink(f"/proc/self/fd/{fd}").encode() + b"\0"
        return real_fcntl(fd, op, *a)

    monkeypatch.setattr(os, "readlink", no_proc)
    monkeypatch.setattr(fcntl, "F_GETPATH", getpath, raising=False)
    monkeypatch.setattr(fcntl, "fcntl", fake_fcntl)
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "a.txt").write_text("TOKEN here\n")
    res = tools._grep_fallback("TOKEN", tmp_path / "proj", None, None)
    assert "a.txt:1:TOKEN here" in res["matches"]
