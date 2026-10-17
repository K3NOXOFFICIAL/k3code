"""A glob in a bash command is walked on disk to see whether it reaches a credential file. The walk is bounded so a
deep pattern that matches nothing cannot stall the daemon's event loop while one decision is made."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from k3code.permissions import decide, engine


def _tree(root: Path, fanout: int, depth: int) -> None:
    if depth == 0:
        return
    for i in range(fanout):
        d = root / f"d{i}"
        d.mkdir()
        _tree(d, fanout, depth - 1)


@pytest.fixture
def scandir_entries(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen = [0]
    real = os.scandir

    class Counting:
        def __init__(self, path: str | os.PathLike[str]) -> None:
            self.it = real(path)

        def __enter__(self) -> Counting:
            return self

        def __exit__(self, *exc: object) -> None:
            self.it.close()

        def __iter__(self):
            for e in sorted(self.it, key=lambda e: e.name):  # a fixed listing order, as the cases below rely on
                seen[0] += 1
                yield e

    def counting(path: str | os.PathLike[str] = ".") -> Counting:
        return Counting(path)

    monkeypatch.setattr(os, "scandir", counting)
    return seen


def test_deep_glob_that_matches_nothing_stops_at_the_entry_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scandir_entries: list[int]
) -> None:
    _tree(tmp_path, fanout=6, depth=4)  # 1554 directories below the root
    monkeypatch.setattr(engine, "_GLOB_MAX_ENTRIES", 200, raising=False)
    pattern = f"{tmp_path}/*/*/*/*/nomatch"
    verdict = engine._classify_word(pattern, str(tmp_path), "/home/user")
    assert scandir_entries[0] <= 200 + 64
    assert verdict == "unchecked"  # the walk gave up: unknown, so a person is asked, never an allow


def test_deep_glob_decides_within_a_fixed_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path, fanout=6, depth=5)  # 9330 directories
    monkeypatch.setattr(engine, "_GLOB_BUDGET_S", 0.0, raising=False)
    pattern = f"{tmp_path}/*/*/*/*/*/nomatch"
    start = time.monotonic()
    verdict = engine._classify_word(pattern, str(tmp_path), "/home/user")
    assert time.monotonic() - start < 1.0
    assert verdict == "unchecked"


def test_ordinary_globs_are_unchanged(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "src" / ".hidden").write_text("x")
    cwd, home = str(tmp_path), "/home/user"
    assert engine._classify_word(f"{tmp_path}/src/*.py", cwd, home) is None
    assert engine._classify_word(f"{tmp_path}/*/*", cwd, home) is None
    assert engine._classify_word(f"{tmp_path}/nothing/*", cwd, home) is None
    assert engine._classify_word(f"{tmp_path}/.en?", cwd, home) == "deny"  # shape of .env


def test_glob_that_reaches_a_credential_on_disk_is_still_denied(tmp_path: Path) -> None:
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_fake").write_text("not a key")
    verdict = engine._classify_word(f"{tmp_path}/ho*/.ssh/id_*", str(tmp_path), str(tmp_path / "home"))
    assert verdict == "deny"


def test_entry_that_cannot_be_stat_ed_does_not_end_the_walk(tmp_path: Path, scandir_entries: list[int]) -> None:
    # `glob` skips an entry whose stat fails and goes on; one such symlink must not hide the credential after it
    for n in range(3):
        (tmp_path / f"l{n}").symlink_to(tmp_path / f"l{n}")
    (tmp_path / "zz").mkdir()
    (tmp_path / "zz" / ".env.staging").write_text("not a secret")
    verdict = engine._classify_word(f"{tmp_path}/*/.env.[s]taging", str(tmp_path), "/home/user")
    assert verdict == "deny"


def test_credential_after_more_than_a_thousand_decoy_matches_is_still_found(
    tmp_path: Path, scandir_entries: list[int]
) -> None:
    for n in range(1200):
        (tmp_path / f".anv.s{n:05d}").write_text("x")
    (tmp_path / ".env.staging").write_text("not a secret")
    assert engine._classify_word(f"{tmp_path}/.[!x]nv.s*", str(tmp_path), "/home/user") == "deny"


def test_spent_budget_is_not_an_allow_in_yolo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scandir_entries: list[int]
) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_fake").write_text("not a key")
    (proj / "a").mkdir(parents=True)
    for n in range(300):
        (proj / "a" / f"f{n:03d}").write_text("x")
    (proj / "keys").symlink_to(home / ".ssh")  # listed after "a", whose 300 files use up the budget
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(engine, "_GLOB_MAX_ENTRIES", 100, raising=False)
    for mode in ("yolo", "auto", "default"):
        d = decide(mode=mode, tool="bash", args={"command": "head */id_*"}, cwd=proj)
        assert d.action in ("deny", "ask"), mode
        assert "too large to check" in (d.message or ""), mode
    assert decide(mode="yolo", tool="bash", args={"command": "head */id_*"}, cwd=proj).action == "deny"
    assert decide(mode="default", tool="bash", args={"command": "head */id_*"}, cwd=proj).needs_human


def test_one_budget_covers_all_the_glob_words_of_a_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scandir_entries: list[int]
) -> None:
    _tree(tmp_path, fanout=6, depth=4)
    monkeypatch.setattr(engine, "_GLOB_MAX_ENTRIES", 200, raising=False)
    cmd = f"head {tmp_path}/*/*/*/{{a,b,c,d}}{{a,b,c,d}}"  # sixteen glob words
    decide(mode="default", tool="bash", args={"command": cmd}, cwd=tmp_path)
    assert scandir_entries[0] <= 200 + 64


def test_very_deep_patterns_decide_without_an_exception(tmp_path: Path) -> None:
    literal = "/" + "/".join(["a"] * 1500) + "/*"
    assert engine._classify_word(literal, str(tmp_path), "/home/user") is None
    wild = "/" + "*/" * 1500 + "x"
    assert engine._classify_word(wild, str(tmp_path), "/home/user") == "unchecked"


def test_ordinary_glob_of_three_thousand_files_decides_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    (proj / "images").mkdir(parents=True)
    (proj / "out").mkdir()
    for n in range(3000):
        (proj / "images" / f"img{n:04d}.png").write_text("x")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for mode in ("yolo", "auto", "default"):
        d = decide(mode=mode, tool="bash", args={"command": "cp images/*.png out/"}, cwd=proj)
        assert "too large" not in (d.message or ""), mode
        assert d.action != "deny", mode
    assert engine._classify_word(f"{proj}/images/*.png", str(proj), str(tmp_path / "home")) is None


def test_secret_dirs_are_resolved_once_per_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "images").mkdir()
    for n in range(300):
        (tmp_path / "images" / f"img{n:04d}.png").write_text("x")
    real, calls = os.path.realpath, [0]

    def counting(p: str, **kw: object) -> str:
        calls[0] += 1
        return real(p, **kw)

    monkeypatch.setattr(os.path, "realpath", counting)
    engine._classify_word(f"{tmp_path}/images/*.png", str(tmp_path), "/home/user")
    assert calls[0] < 300 * 3 + 20  # a hit costs its own realpath, not one per secret directory


def test_unchecked_glob_in_a_write_or_delete_position_asks_with_the_right_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scandir_entries: list[int]
) -> None:
    proj = tmp_path / "proj"
    (proj / "a").mkdir(parents=True)
    for n in range(300):
        (proj / "a" / f"f{n:03d}").write_text("x")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(engine, "_GLOB_MAX_ENTRIES", 100, raising=False)
    for cmd in ("rm a/*", "cp x a/*"):
        d = decide(mode="auto", tool="bash", args={"command": cmd}, cwd=proj)
        assert d.needs_human, cmd
        assert "too large to check" in (d.message or ""), cmd
    # a credential delete or overwrite is allowed in yolo as it always was; an unchecked one is not refused either
    assert decide(mode="yolo", tool="bash", args={"command": "rm a/*"}, cwd=proj).action == "allow"
