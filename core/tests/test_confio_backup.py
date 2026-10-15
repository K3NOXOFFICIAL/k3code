"""write_yaml keeps a ``.bak-*`` of the previous file only when the write changes it."""

from __future__ import annotations

from pathlib import Path

from k3code.confio import write_yaml


def test_identical_rewrite_makes_no_backup(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    assert write_yaml(cfg, {"a": 1}) is None  # nothing to back up the first time
    for _ in range(3):
        assert write_yaml(cfg, {"a": 1}) is None
    assert not list(tmp_path.glob("config.yaml.bak-*"))


def test_changed_rewrite_backs_up_the_previous_content(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    write_yaml(cfg, {"a": 1})
    bak = write_yaml(cfg, {"a": 2})
    assert bak is not None and bak.read_text() == "a: 1\n"
    assert cfg.read_text() == "a: 2\n"
    assert len(list(tmp_path.glob("config.yaml.bak-*"))) == 1
