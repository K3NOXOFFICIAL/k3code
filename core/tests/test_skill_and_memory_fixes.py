"""Skill frontmatter that real files get wrong, and the skill tool's size contract."""

from __future__ import annotations

import os
from pathlib import Path

from k3code import skills
from k3code.extratools import MAX_SKILL_CHARS, register_skill_tool
from k3code.providers.types import Message
from k3code.tools import ToolRegistry, clip_tool_results


def skills_home() -> Path:
    return Path(
        os.environ["K3CODE_HOME"]
    )  # the user's skills are always trusted (the project's wait for `k3code trust`)


def test_frontmatter_without_a_name_value_a_bom_and_an_unquoted_colon():
    assert skills.parse_frontmatter("---\nname:\ndescription: d\n---\nbody") == {"description": "d"}
    assert skills.parse_frontmatter("﻿---\nname: n\ndescription: d\n---\nbody") == {"name": "n", "description": "d"}
    loose = skills.parse_frontmatter("---\nname: n\ndescription: Use when: the user asks\n---\nbody")
    assert loose == {"name": "n", "description": "Use when: the user asks"}
    assert skills.parse_frontmatter('---\nname: n\ndescription: "Quoted: yes"\n---\n') == {
        "name": "n",
        "description": "Quoted: yes",
    }
    assert skills.parse_frontmatter("no frontmatter") == {}


def test_a_skill_directory_with_a_broken_header_is_still_named_and_described(tmp_path: Path):
    root = skills_home() / "skills"
    for folder, header in (("bare", "name:\ndescription: bare one"), ("colon", "name: colon\ndescription: A: b")):
        (root / folder).mkdir(parents=True)
        (root / folder / "SKILL.md").write_text(f"---\n{header}\n---\nbody\n")
    found = {s.name: s.description for s in skills.discover(tmp_path) if s.name in ("bare", "colon")}
    assert found == {"bare": "bare one", "colon": "A: b"}  # not the skill "None"


async def test_a_long_skill_reaches_the_model_whole_up_to_its_own_cap(tmp_path: Path):
    folder = skills_home() / "skills" / "big"
    folder.mkdir(parents=True)
    body = "".join(f"step {i}\n" for i in range(6000))  # ~50k chars
    (folder / "SKILL.md").write_text(f"---\nname: big\ndescription: d\n---\n{body}")
    reg = ToolRegistry()
    register_skill_tool(reg, tmp_path, [])
    _, handler = reg.get("skill")
    result = await handler({"name": "big"}, cwd=tmp_path)
    assert len(body) < MAX_SKILL_CHARS and "step 5999" in result["content"]
    (sent,) = clip_tool_results([Message(role="tool", content=result["content"], tool_call_id="1", name="skill")])
    assert sent.content == result["content"]  # not cut to the 10k a generic tool result gets


def test_a_file_imported_by_an_earlier_one_is_not_loaded_twice(tmp_path: Path):
    from k3code.memory import load_memory, memory_prompt

    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("shared rule one\nshared rule two\n")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\nclaude only line\n")
    files = load_memory(tmp_path)
    prompt = memory_prompt(tmp_path)
    assert prompt.count("shared rule one") == 1 and "claude only line" in prompt
    assert len([f for f in files if f.scope == "project"]) >= 1


def test_memory_with_invalid_utf8_can_still_be_appended_to(tmp_path: Path):
    from k3code.memory import append_memory

    path = tmp_path / "K3CODE.md"
    path.write_bytes(b"- caf\xe9 note\n")
    append_memory(path, "new fact")
    assert "new fact" in path.read_text(encoding="utf-8")


def test_a_custom_output_style_loses_its_frontmatter_and_may_be_latin1(tmp_path: Path):
    from k3code.outputstyle import style_text

    styles = Path(os.environ["K3CODE_HOME"]) / "output-styles"
    styles.mkdir(parents=True)
    (styles / "terse.md").write_text("---\nname: terse\ndescription: short\n---\nBe terse.\n")
    (styles / "latin.md").write_bytes(b"Caf\xe9 style.\n")
    assert style_text("terse", tmp_path) == "Be terse."
    assert style_text("latin", tmp_path).startswith("Caf")  # no UnicodeDecodeError in build_system_prompt


def test_an_artifact_id_must_name_exactly_one_artifact(tmp_path: Path):
    from k3code.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / "a.db")
    first = store.register("plan", tmp_path / "x.md")
    store._db.execute("UPDATE artifacts SET id = 'abc12345' WHERE id = ?", (first.id,))
    second = store.register("plan", tmp_path / "y.md")
    store._db.execute("UPDATE artifacts SET id = 'abc99999' WHERE id = ?", (second.id,))
    store._db.commit()
    assert store.get("") is None and store.get("%") is None and store.get("_") is None
    assert store.get("abc") is None and {a.id for a in store.matches("abc")} == {"abc12345", "abc99999"}
    assert store.get("abc1").id == "abc12345" and store.get("abc99999").id == "abc99999"


def test_usage_db_indexes_the_turn_column_even_in_an_old_database(tmp_path: Path):
    import sqlite3

    from k3code.usage import UsageDB

    path = tmp_path / "usage.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE events (ts REAL NOT NULL, day TEXT NOT NULL, session TEXT NOT NULL DEFAULT '', kind TEXT NOT"
        " NULL, provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', tokens_in INTEGER NOT NULL"
        " DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost_usd REAL, seconds REAL NOT NULL DEFAULT 0,"
        " detail TEXT NOT NULL DEFAULT '')"
    )
    old.commit()
    old.close()
    db = UsageDB(path)
    db.record("call", tokens_in=5, tokens_out=1, turn="t1")
    assert db.turn_tokens("t1") == (5, 1)
    plan = " ".join(str(r) for r in db._db.execute("EXPLAIN QUERY PLAN SELECT 1 FROM events WHERE turn = 't1'"))
    assert "events_turn" in plan
    db.close()


def test_the_note_k3code_writes_into_user_memory_does_not_reach_the_prompt(tmp_path: Path):
    from k3code.memory import memory_prompt, user_memory_path

    path = user_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "- I like short answers.\n"
        "<!-- managed by k3code; rewritten automatically, edit outside this section -->\n"
        "- Prefers uv over pip.\n"
    )
    prompt = memory_prompt(tmp_path)
    assert "Prefers uv over pip." in prompt and "I like short answers." in prompt and "managed by k3code" not in prompt


def test_the_skills_index_does_not_change_when_a_skill_is_used(tmp_path: Path):
    from k3code.learning.curator import record_use

    root = skills_home() / "skills"
    for i in range(70):  # past the 60 that fit: the index has to choose
        (root / f"s{i:02d}").mkdir(parents=True)
        (root / f"s{i:02d}" / "SKILL.md").write_text(f"---\nname: s{i:02d}\ndescription: skill {i}\n---\nbody\n")
    before = skills.skills_prompt(tmp_path)
    record_use("s69", ok=True)  # a skill outside the index is loaded through the skill tool
    assert skills.skills_prompt(tmp_path) == before
