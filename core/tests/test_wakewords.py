"""Wake-word detection: the shared table (also run by the TUI's wake-words test) and the rules it cannot show."""

from __future__ import annotations

import json
import time
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


def _timed(text: str) -> tuple[object, float]:
    start = time.perf_counter()
    return detect(text), time.perf_counter() - start


def test_a_long_prompt_full_of_code_is_scanned_in_linear_time() -> None:
    # detect() runs on the gateway's event loop for every typed prompt, and collapsed pastes reach it fully expanded.
    # Checking every code span against every other span made these take seconds to minutes (quadratic).
    got, took = _timed("```" * 20000 + " ultracode")  # 60 KB of fences, then a word outside them
    assert got is not None and got.mode == "ultracode" and took < 2.0
    got, took = _timed("`a` " * 20000 + "ultracode " * 20000)  # 280 KB: 20000 inline spans, then 20000 words
    assert got is not None and got.mode == "ultracode" and got.start == 80000 and took < 2.0
    got, took = _timed("```" * 20000)  # no word at all
    assert got is None and took < 2.0
    # every word inside a code span: nothing triggers, however many there are
    got, took = _timed("`ultracode` " * 20000 + "```\n" + "ultraplan\n" * 20000)
    assert got is None and took < 2.0
