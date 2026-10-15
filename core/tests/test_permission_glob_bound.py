"""A glob in a bash command is walked on disk to see whether it reaches a credential file. The walk is bounded so a
deep pattern that matches nothing cannot stall the daemon's event loop while one decision is made."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from k3code.permissions import engine


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
            for e in self.it:
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
    assert verdict == "human"  # the walk gave up: unknown, so a person is asked, never an allow


def test_deep_glob_decides_within_a_fixed_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path, fanout=6, depth=5)  # 9330 directories
    monkeypatch.setattr(engine, "_GLOB_BUDGET_S", 0.0, raising=False)
    pattern = f"{tmp_path}/*/*/*/*/*/nomatch"
    start = time.monotonic()
    verdict = engine._classify_word(pattern, str(tmp_path), "/home/user")
    assert time.monotonic() - start < 1.0
    assert verdict == "human"


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
