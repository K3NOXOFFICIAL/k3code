"""Issue #141: ``learning.enabled: false`` keeps learned blocks out of the prompt, and ``/learned`` lists, forgets and
purges what learning stored."""

from __future__ import annotations

from pathlib import Path

from k3code import prompting
from k3code.config import Settings
from k3code.memory import user_memory_path, write_learned


def _seed_prompt_state(home: Path, cwd: Path) -> None:
    user_memory_path().parent.mkdir(parents=True, exist_ok=True)
    user_memory_path().write_text("- I write my own notes\n", encoding="utf-8")
    write_learned(cwd, ["build with make all"])
    (home / "overlays").mkdir(parents=True)
    (home / "overlays" / "x1-terse.md").write_text("answer tersely", encoding="utf-8")


def test_learning_off_leaves_learned_notes_and_overlays_out_of_the_prompt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("K3CODE_HOME", str(home))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _seed_prompt_state(home, cwd)

    on = dict(prompting.prompt_sections("base", cwd=cwd, config=Settings()))
    assert "build with make all" in on["memory"] and "answer tersely" in on["overlay"]

    off = dict(prompting.prompt_sections("base", cwd=cwd, config=Settings(learning={"enabled": False})))
    assert "build with make all" not in off["memory"] and "Learned project notes" not in off["memory"]
    assert "overlay" not in off
    assert "I write my own notes" in off["memory"]  # the user's own memory is not learned
