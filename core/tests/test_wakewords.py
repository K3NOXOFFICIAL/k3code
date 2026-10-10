"""Wake-word detection: the shared table (also run by the TUI's wake-words test) and the rules it cannot show."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from k3code.wakewords import WAKE_WORDS, detect

CASES = json.loads((Path(__file__).parent / "data" / "wake_word_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["input"][:40] or "<empty>" for c in CASES])
def test_table(case: dict) -> None:
    got = detect(case["input"])
    assert (got.mode if got else None) == case["mode"]
    assert (got.task if got else None) == case["task"]


def test_table_covers_every_mode_and_a_miss() -> None:
    assert {c["mode"] for c in CASES} >= {*WAKE_WORDS, None}


def test_offsets_point_at_the_word() -> None:
    text = "please fix the build, Ultracode"
    got = detect(text)
    assert got is not None
    assert text[got.start : got.end] == "Ultracode"
    assert got.mode == "ultracode"


def test_every_wake_word_names_a_command() -> None:
    from k3code.commands.builtin import build_registry

    registry = build_registry()
    for word, command in WAKE_WORDS.items():
        assert word == command
        assert registry.get(command) is not None
