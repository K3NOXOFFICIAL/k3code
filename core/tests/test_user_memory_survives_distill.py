"""Issue #91: lines added with ``/memory add --user`` must survive every daily distill."""

from __future__ import annotations

import json
from types import SimpleNamespace

from k3code.commands.memory_cmd import MemoryCommand
from k3code.learning import distiller
from k3code.learning.decisions import DecisionLog
from k3code.memory import append_memory, user_memory_path
from learn_helpers import FakeCaller

HEADING = distiller.HEADING


def seed(log: DecisionLog) -> None:
    for _ in range(3):
        log.record(
            "model_switch",
            subject="m-x -> m-y",
            choice="m-y",
            detail={"from": "m-x", "to": "m-y", "task_kind": "review", "to_tier": "strong"},
        )


def pref(text: str = "avoids model m-x") -> distiller.Preference:
    return distiller.Preference(text, 0.8, 5, "k")


def test_issue_repro_two_adds_two_distills(tmp_path):
    p = tmp_path / "USER.md"
    p.write_text("# Me\nalways answer in German\n", encoding="utf-8")
    distiller.write_auto_section(p, [pref()])
    append_memory(p, "never use tabs")
    distiller.write_auto_section(p, [pref()])
    append_memory(p, "prefer small commits")
    distiller.write_auto_section(p, [pref("avoids model m-z")])
    text = p.read_text(encoding="utf-8")
    assert "- never use tabs\n" in text and "- prefer small commits\n" in text
    assert "always answer in German" in text
    assert "avoids model m-z" in text and "avoids model m-x" not in text
    assert distiller.read_auto_section(p) == ["avoids model m-z"]


async def test_distill_keeps_added_lines_across_repeated_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    log = DecisionLog(tmp_path)
    seed(log)
    md = user_memory_path()
    md.parent.mkdir(parents=True)
    md.write_text("# Me\nI like tabs.\n", encoding="utf-8")  # no managed block yet
    polish = FakeCaller('[{"key": "avoid:m-x:review", "text": "dislikes m-x for reviews"}]')
    res = await MemoryCommand().handle(_Ctx(), None, "add --user first note")
    assert "Added" in res["output"]
    await distiller.distill(log, home=tmp_path, user_md=md, caller=polish)
    await MemoryCommand().handle(_Ctx(), None, "add --user second note")
    await distiller.distill(log, home=tmp_path, user_md=md)
    await distiller.distill(log, home=tmp_path, user_md=md)
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# Me\nI like tabs.\n- first note\n")
    assert "- second note\n" in text
    assert text.count(HEADING) == 1
    assert distiller.read_auto_section(md) == ["avoids model m-x for review tasks"]


async def test_text_outside_the_block_is_byte_for_byte_untouched(tmp_path):
    log = DecisionLog(tmp_path)
    seed(log)
    md = tmp_path / "USER.md"
    before = "# Me\nweird  spacing\t\n\n\n- mine\n"
    md.write_bytes(before.encode())
    await distiller.distill(log, home=tmp_path, user_md=md)
    first = md.read_bytes().decode()
    assert first.startswith(before)
    after = "\n- added later, no heading after it\nmore text, no trailing newline"  # no heading after it
    md.write_bytes((first + after).encode())
    await distiller.distill(log, home=tmp_path, user_md=md)
    now = md.read_bytes().decode()
    assert now.startswith(before) and now.endswith(after)
    assert now.count(HEADING) == 1


async def test_distill_survives_a_malformed_preferences_json(tmp_path):
    log = DecisionLog(tmp_path)
    seed(log)
    (tmp_path / "learning").mkdir(exist_ok=True)
    md = tmp_path / "USER.md"
    for bad in (
        "[]",
        "null",
        '{"k": 1}',
        '{"k": {"confidence": 1}}',
        '{"k": {"text": "t", "confidence": "x", "evidence": 1}}',
    ):
        (tmp_path / "learning" / "preferences.json").write_text(bad, encoding="utf-8")
        await distiller.distill(log, home=tmp_path, user_md=md)  # must repair the file, not raise
        assert distiller.read_auto_section(md) == ["avoids model m-x for review tasks"]


def test_read_auto_section_ignores_lines_after_the_block(tmp_path):
    p = tmp_path / "USER.md"
    distiller.write_auto_section(p, [pref()])
    append_memory(p, "mine")
    assert distiller.read_auto_section(p) == ["avoids model m-x"]


async def test_old_format_file_is_migrated_without_losing_anything(tmp_path):
    """A USER.md written before the managed block had an end marker: its lines are user text unless the last distill
    wrote them (preferences.json), and a line added after the first distill sits inside the old section."""
    log = DecisionLog(tmp_path)
    seed(log)
    md = tmp_path / "USER.md"
    md.write_text(
        "# Me\nI like tabs.\n\n## Learned preferences (auto)\n"
        "<!-- managed by k3code; rewritten automatically, edit outside this section -->\n"
        "- machine line from last time\n- never use tabs\n\n## My notes\nkeep this\n",
        encoding="utf-8",
    )
    (tmp_path / "learning").mkdir(exist_ok=True)
    (tmp_path / "learning" / "preferences.json").write_text(
        json.dumps({"k": {"text": "machine line from last time", "confidence": 0.8, "evidence": 5}}), encoding="utf-8"
    )
    await distiller.distill(log, home=tmp_path, user_md=md)
    text = md.read_text(encoding="utf-8")
    assert "- never use tabs\n" in text and "I like tabs." in text and "## My notes\nkeep this" in text
    assert "machine line from last time" not in text
    assert text.index("- never use tabs") < text.index(HEADING) < text.index("## My notes")
    again = md.read_text(encoding="utf-8")
    await distiller.distill(log, home=tmp_path, user_md=md)
    assert md.read_text(encoding="utf-8") == again  # a second run changes nothing


def test_old_format_file_without_preferences_json_loses_nothing(tmp_path):
    md = tmp_path / "USER.md"
    md.write_text("# Me\n## Learned preferences (auto)\n- unknown origin\n", encoding="utf-8")
    distiller.write_auto_section(md, [pref()])
    assert "- unknown origin\n" in md.read_text(encoding="utf-8")
    assert distiller.read_auto_section(md) == ["avoids model m-x"]


class _Ctx(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(config=SimpleNamespace(mem0=SimpleNamespace(url="")), sessions=None)
