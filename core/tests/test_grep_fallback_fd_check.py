"""The grep fallback decides on the file it opened, not on the path it checked a moment before (K3NOX-663, K3NOX-657).

A background job can swap a directory of the project for a link to a directory outside every root while
grep runs; a check by path name followed by an open by path name loses that race. A FIFO in the tree has no writer,
so a plain ``open`` blocks the search thread for good.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import k3code.tools as tools

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
    assert "stopped after" not in out.get("warnings", "")


def test_a_file_swapped_for_a_link_after_the_walk_is_not_read(tmp_path, fallback_only) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text(f"TOKEN={SECRET}\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("TOKEN in a\n")
    real_base = os.path.realpath(proj)
    lines = list(tools._grep_file(proj / "a.txt", re.compile("TOKEN"), 1e18, expect=f"{real_base}/a.txt"))
    assert lines == [f"{proj / 'a.txt'}:1:TOKEN in a"]
    (proj / "a.txt").unlink()
    (proj / "a.txt").symlink_to(secret)
    with pytest.raises(OSError):
        list(tools._grep_file(proj / "a.txt", re.compile("TOKEN"), 1e18, expect=f"{real_base}/a.txt"))


def test_a_walked_file_whose_open_fd_points_elsewhere_is_not_read(tmp_path, fallback_only) -> None:
    """A parent directory swapped for a link: the name looks right, the open file is not the one expected."""
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "a.txt").write_text(f"TOKEN={SECRET}\n")
    (tmp_path / "proj").symlink_to(tmp_path / "elsewhere")
    expect = os.path.join(os.path.realpath(tmp_path), "proj", "a.txt")  # what the walk listed
    with pytest.raises(OSError):
        list(tools._grep_file(tmp_path / "proj" / "a.txt", re.compile("TOKEN"), 1e18, expect=expect))
