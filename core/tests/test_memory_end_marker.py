"""The markers k3code writes around the learned-preferences block of USER.md are for whoever edits the file: the note
above the block and the line that ends it. Neither goes to the model (the end marker cost tokens on every request)."""

from __future__ import annotations

from pathlib import Path

from k3code.learning.distiller import _END, Preference, write_auto_section
from k3code.memory import append_memory, memory_prompt, user_memory_path


def test_the_end_marker_of_the_learned_block_does_not_reach_the_prompt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    path = user_memory_path()
    write_auto_section(path, [Preference("prefers pytest -q", 0.9, 5, "k")])
    append_memory(path, "my own line")  # after the block: the user's
    assert _END in path.read_text(encoding="utf-8")  # the file keeps it
    prompt = memory_prompt(tmp_path)
    assert "prefers pytest -q" in prompt and "my own line" in prompt
    assert "k3code:auto" not in prompt and "managed by k3code" not in prompt


def test_only_the_marker_line_goes_a_mention_inside_a_line_stays(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    path = user_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"- first\n  {_END}  \n- the line {_END} closes the block\n", encoding="utf-8")
    assert memory_prompt(tmp_path) == f"## User memory\n\n- first\n- the line {_END} closes the block"


def test_the_setup_begin_and_end_lines_do_not_reach_the_prompt(tmp_path: Path, monkeypatch) -> None:
    from k3code.setup.steps import USER_MD_BEGIN, USER_MD_END

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    path = user_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{USER_MD_BEGIN}\n- answers in German\n{USER_MD_END}\n- my own line\n", encoding="utf-8")
    assert memory_prompt(tmp_path) == "## User memory\n\n- answers in German\n- my own line"
