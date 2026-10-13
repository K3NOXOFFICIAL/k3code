"""The base system prompt must not promise what the harness does not do (GitHub #166, finding 1)."""

from __future__ import annotations

from k3code.prompting import base_prompt


def _line_with(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if needle in line)


def test_prompt_does_not_say_reads_never_need_permission():
    text = base_prompt().lower()
    assert "don't require permission" not in text
    assert "do not require permission" not in text
    # reads outside the project and credential paths can be refused: the prompt says so
    line = _line_with(text, "outside the project").lower()
    assert "refused" in line or "ask" in line


def test_prompt_does_not_say_a_read_file_never_changes():
    text = base_prompt().lower()
    assert "is unchanged unless you edited it" not in text
    assert "don't re-read it" not in text
    line = _line_with(text, "re-read")
    assert "bash" in line and "edit" in line


def test_prompt_tells_the_model_what_to_do_when_nobody_can_answer():
    assert "reversible option" in base_prompt().lower()
