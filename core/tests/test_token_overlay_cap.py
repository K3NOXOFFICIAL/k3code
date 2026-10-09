"""Prompt overlays are capped: every overlays/*.md used to be injected into the system prompt, re-sent on every call."""

from __future__ import annotations

from k3code.learning import optimizer


def test_overlays_are_capped_by_count_and_size_with_a_note(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    d = tmp_path / "overlays"
    d.mkdir()
    for i in range(15):
        (d / f"o{i:02d}.md").write_text(f"guidance number {i}")
    text = optimizer.overlay_prompt()
    assert "guidance number 0" in text and "guidance number 9" in text
    assert "guidance number 10" not in text  # only the first 10 files
    assert "5 more overlays not included" in text


def test_overlays_are_capped_by_total_chars(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    d = tmp_path / "overlays"
    d.mkdir()
    for i in range(3):
        (d / f"o{i}.md").write_text(f"{i}" * 1800)
    text = optimizer.overlay_prompt()
    assert "0" * 1800 in text and "1" * 1800 in text
    assert "2" * 1800 not in text  # a third one would pass 4000 chars
    assert "1 more overlay not included" in text
    assert len(text) < 4000 + 300


def test_no_overlays_means_no_section(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    assert optimizer.overlay_prompt() == ""
