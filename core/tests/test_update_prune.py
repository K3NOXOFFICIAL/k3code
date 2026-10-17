"""`k3code update` prunes old version dirs (#158, and the prune-order item of the install checklist #160).

prune() used to sort by version name, so the `-src.<sha>` suffix decided which builds survived: it kept an old build
and deleted a newer one. It also deleted the dir that a running daemon or TUI executes from, and the last version
that still had the k3 binary once newer builds had lost it. Everything runs against temporary data dirs."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from k3code import update as upd

needs_proc = pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setenv("K3CODE_DATA", str(d))
    (d / "versions").mkdir(parents=True)
    return d


def build(data: Path, name: str, *, age: int, k3: bool = False) -> Path:
    """A finished build `versions/<name>`, `age` seconds old (the mtimes are set, so the order never depends on how
    fast the test creates them)."""
    vdir = data / "versions" / name
    vdir.mkdir()
    (vdir / ".complete").write_text(name + "\n")
    if k3:
        (vdir / "bin").mkdir()
        (vdir / "bin" / "k3").write_text("binary\n")
    when = time.time() - age
    os.utime(vdir / ".complete", (when, when))
    os.utime(vdir, (when, when))
    return vdir


def names(data: Path) -> set[str]:
    return {p.name for p in (data / "versions").iterdir()}


def test_prune_keeps_the_newest_builds_not_the_highest_shas(data: Path) -> None:
    """The audit's run: installed 28eaf5e -> cec7fa0 -> 46edf15 -> d67e7c7 -> e86dd77. Sorted by name, prune(3) kept
    cec7fa0 and deleted the newer 46edf15."""
    order = ["28eaf5e", "cec7fa0", "46edf15", "d67e7c7", "e86dd77"]
    for i, sha in enumerate(order):
        build(data, f"0.1.0-src.{sha}", age=(len(order) - i) * 1000)
    upd.switch_to("0.1.0-src.d67e7c7")
    upd.switch_to("0.1.0-src.e86dd77")
    removed = upd.prune(keep=3)
    assert names(data) == {f"0.1.0-src.{s}" for s in ("46edf15", "d67e7c7", "e86dd77")}
    assert sorted(removed) == ["0.1.0-src.28eaf5e", "0.1.0-src.cec7fa0"]


def test_prune_never_removes_the_current_or_previous_version_however_old(data: Path) -> None:
    for name, age in (("1.0.0", 9000), ("1.1.0", 3000), ("1.2.0", 2000), ("1.3.0", 1000)):
        build(data, name, age=age)
    upd.switch_to("1.0.0")
    upd.switch_to("1.3.0")
    # current 1.3.0, previous 1.0.0 (the oldest by far)
    assert upd.prune(keep=1) == ["1.2.0", "1.1.0"]
    assert names(data) == {"1.0.0", "1.3.0"}


# -- versions that a running process executes from --------------------------------


def _proc_entry(
    proc: Path, pid: str, *, cmdline: tuple[str, ...] | list[str] = (), exe: Path | None = None, cwd: Path | None = None
) -> None:
    d = proc / pid
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in cmdline))
    if exe:
        (d / "exe").symlink_to(exe)
    if cwd:
        (d / "cwd").symlink_to(cwd)


def test_running_versions_reads_exe_cwd_and_cmdline_of_every_process(data: Path, tmp_path: Path) -> None:
    vers = data / "versions"
    proc = tmp_path / "proc"
    _proc_entry(proc, "101", exe=vers / "V1" / "venv" / "bin" / "python3.12")
    _proc_entry(proc, "102", cwd=vers / "V2" / "tui")
    # a venv python's exe is the base interpreter: the version only shows in the command line
    _proc_entry(proc, "103", cmdline=[str(vers / "V3" / "venv" / "bin" / "python"), "-m", "k3code", "daemon"])
    _proc_entry(proc, "104", cmdline=["node", f"--import={vers}/V4/tui/dist/hook.js", "entry.js"])
    _proc_entry(proc, "105", cmdline=["/usr/bin/python3", "unrelated"], exe=Path("/usr/bin/python3"), cwd=tmp_path)
    assert upd.running_versions(proc) == {"V1", "V2", "V3", "V4"}


def test_running_versions_ignores_what_it_cannot_read(data: Path, tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _proc_entry(proc, "201", cmdline=[str(data / "versions" / "V1" / "venv" / "bin" / "python")])
    (proc / "202").write_text("a file, not a process directory\n")
    (proc / "202x").mkdir()  # not a pid
    (proc / "self").symlink_to(proc / "201")
    _proc_entry(proc, "203")  # a kernel thread: empty command line, no exe, no cwd
    (proc / "204").mkdir()  # exited between the listing and the read
    assert upd.running_versions(proc) == {"V1"}
    assert upd.running_versions(tmp_path / "no-such-proc") == set()


def test_running_versions_matches_the_data_dir_under_either_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    (real / "versions").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("K3CODE_DATA", str(link))
    proc = tmp_path / "proc"
    _proc_entry(proc, "301", cmdline=[str(real / "versions" / "A" / "venv" / "bin" / "python")])
    _proc_entry(proc, "302", cmdline=[str(link / "versions" / "B" / "venv" / "bin" / "python")])
    assert upd.running_versions(proc) == {"A", "B"}


@needs_proc
def test_prune_keeps_the_version_a_running_process_has_as_its_cwd(data: Path) -> None:
    """#158: the updater deleted the version dir of a running TUI; its lazy imports then failed."""
    old = build(data, "1.0.0", age=4000)
    build(data, "1.1.0", age=3000)
    build(data, "1.2.0", age=2000)
    build(data, "1.3.0", age=1000)
    upd.switch_to("1.2.0")
    upd.switch_to("1.3.0")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=old)
    try:
        assert upd.prune(keep=1) == ["1.1.0"]
        assert names(data) == {"1.0.0", "1.2.0", "1.3.0"}
    finally:
        proc.kill()
        proc.wait()
    assert upd.prune(keep=1) == ["1.0.0"]  # once nothing runs from it, it goes like any old version


@needs_proc
def test_prune_keeps_the_version_named_in_a_running_command_line(data: Path) -> None:
    old = build(data, "1.0.0", age=4000)
    build(data, "1.1.0", age=3000)
    build(data, "1.2.0", age=2000)
    upd.switch_to("1.1.0")
    upd.switch_to("1.2.0")
    script = old / "venv" / "bin" / "k3code"  # the shebang python is the base interpreter; the path is an argument
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(script)])
    try:
        assert upd.prune(keep=1) == []
        assert "1.0.0" in names(data)
    finally:
        proc.kill()
        proc.wait()


# -- the last version with the k3 binary ----------------------------------------------


def test_prune_keeps_the_last_version_with_k3_when_the_current_one_lost_it(data: Path) -> None:
    """#95: two updates in a row built without k3; the second pruned the last version that had it."""
    build(data, "0.1.0-src.aaaaaaa", age=5000, k3=True)
    build(data, "0.1.0-src.bbbbbbb", age=4000)
    build(data, "0.1.0-src.ccccccc", age=3000)
    build(data, "0.1.0-src.ddddddd", age=2000)
    build(data, "0.1.0-src.eeeeeee", age=1000)
    upd.switch_to("0.1.0-src.ddddddd")
    upd.switch_to("0.1.0-src.eeeeeee")
    removed = upd.prune(keep=3)
    assert removed == ["0.1.0-src.bbbbbbb"]
    assert "0.1.0-src.aaaaaaa" in names(data)


def test_prune_does_not_hoard_old_k3_builds_while_the_current_version_has_it(data: Path) -> None:
    build(data, "1.0.0", age=4000, k3=True)
    build(data, "1.1.0", age=3000)
    build(data, "1.2.0", age=2000)
    build(data, "1.3.0", age=1000, k3=True)
    upd.switch_to("1.2.0")
    upd.switch_to("1.3.0")
    assert sorted(upd.prune(keep=2)) == ["1.0.0", "1.1.0"]


# -- a staged version that fails its smoke test ------------------------------------------


def test_a_staged_version_that_fails_the_smoke_test_is_removed(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build(data, "1.0.0", age=2000)
    build(data, "1.1.0", age=1000)
    upd.switch_to("1.0.0")
    monkeypatch.setattr(upd, "running_versions", lambda: set())
    res = upd.activate("1.1.0", smoke=lambda _v: (False, "broken"), carry=lambda *_a: None)
    assert not res.ok and names(data) == {"1.0.0"}


def test_a_failed_staged_version_that_a_process_runs_from_is_kept(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#158: the installer reuses an installed build of the same commit, so the staged dir can be the one an old TUI
    or daemon still runs from; removing it after a failed smoke test broke that process's lazy imports."""
    build(data, "1.0.0", age=3000)
    build(data, "1.1.0", age=2000)
    build(data, "1.2.0", age=1000)
    upd.switch_to("1.1.0")
    upd.switch_to("1.2.0")  # previous 1.1.0; a TUI still runs 1.0.0, which the installer handed back as staged
    monkeypatch.setattr(upd, "running_versions", lambda: {"1.0.0"})
    res = upd.activate("1.0.0", smoke=lambda _v: (False, "broken"), carry=lambda *_a: None)
    assert not res.ok and upd.current_version() == "1.2.0"
    assert names(data) == {"1.0.0", "1.1.0", "1.2.0"}
