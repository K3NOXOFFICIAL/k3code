"""A finished turn leaves no transcript checkpoint, the journal directory has a size cap, and sessions.db gives back
the pages that deleted transcripts used to occupy."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from k3code import sqlstore
from k3code.agent.loop import AgentLoop
from k3code.gateway.sessions import SessionStore
from k3code.providers.types import Message, StreamEvent, ToolCall, ToolSpec, Usage
from k3code.reliability.journal import prune_journals
from k3code.router import Router, build_chain

PARAMS = {"type": "object", "properties": {}}


class ScriptedProvider:
    def __init__(self, replies: list[Message]):
        self._replies = replies
        self._n = 0
        self.name = "fake"
        self.base_url = "https://fake.test"

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        msg = self._replies[self._n] if self._n < len(self._replies) else Message(role="assistant", content="done")
        self._n += 1
        yield StreamEvent(type="done", message=msg, usage=Usage())

    async def aclose(self):
        pass


def make_loop(tmp: Path, replies: list[Message]) -> AgentLoop:
    router = Router(build_chain([ScriptedProvider(replies)], [["fake-model"]]), max_retries=0)
    loop = AgentLoop(router, system_prompt="t", max_turns=5, cwd=tmp, permission_mode="yolo", session="ckpt")

    async def handler(args, cwd=None):
        return {"content": "ok"}

    loop.tools.register(ToolSpec(name="probe", description="p", parameters=PARAMS, side_effect=False), handler)
    return loop


def tool_reply() -> Message:
    return Message(role="assistant", content=None, tool_calls=[ToolCall(id="c0", name="probe", arguments={})])


@pytest.mark.asyncio
async def test_a_clean_finish_removes_the_transcript_checkpoint(tmp_path):
    loop = make_loop(tmp_path, [tool_reply(), Message(role="assistant", content="all done")])
    async for _ in loop.run("go"):
        pass
    assert loop.reliability.load_transcript() is None


@pytest.mark.asyncio
async def test_an_interrupted_turn_keeps_the_checkpoint_for_resume(tmp_path):
    loop = make_loop(tmp_path, [tool_reply(), Message(role="assistant", content="all done")])
    gen = loop.run("go")
    async for event in gen:
        if event.type == "done" and event.message and event.message.role == "tool":
            break  # the process dies here: the tool result is checkpointed, the turn never finished
    await gen.aclose()
    saved = loop.reliability.load_transcript()
    assert saved is not None and [m.role for m in saved] == ["system", "user", "assistant"]


def _journal(home: Path, name: str, size: int, age_s: float, now: float) -> Path:
    path = home / "journal" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (now - age_s, now - age_s))
    return path


def test_the_journal_directory_is_capped_by_size_oldest_closed_files_first(tmp_path):
    now = 1_000_000.0
    oldest = _journal(tmp_path, "aaa.jsonl", 400, 300, now)
    middle = _journal(tmp_path, "bbb.jsonl", 400, 200, now)
    newest = _journal(tmp_path, "ccc.jsonl", 400, 100, now)
    removed = prune_journals(tmp_path, keep=lambda sid: False, max_age_s=10_000, max_bytes=900, now=now)
    assert removed == 1
    assert not oldest.exists() and middle.exists() and newest.exists()


def test_the_size_cap_never_touches_a_live_session(tmp_path):
    now = 1_000_000.0
    live = _journal(tmp_path, "live.jsonl", 1000, 300, now)
    closed = _journal(tmp_path, "gone.jsonl", 100, 100, now)
    prune_journals(tmp_path, keep=lambda sid: sid == "live", max_age_s=10_000, max_bytes=500, now=now)
    assert live.exists() and not closed.exists()


def _fat_store(tmp_path: Path) -> SessionStore:
    store = SessionStore(tmp_path / "s.db")
    for i in range(20):
        sess = store.create(title=f"t{i}", cwd="/p")
        sess.messages = [{"role": "user", "content": "x" * 400_000}]
        store.save(sess)
    return store


def test_deleting_sessions_then_compacting_shrinks_the_file(tmp_path):
    store = _fat_store(tmp_path)
    for row in store.list(limit=50):
        store.delete(row.session_id)
    store._db.commit()
    before = (tmp_path / "s.db").stat().st_size
    freed = store.compact()
    after = (tmp_path / "s.db").stat().st_size
    assert freed > 0 and after < before / 4, (before, after)


def test_a_new_store_vacuums_incrementally(tmp_path):
    store = SessionStore(tmp_path / "s.db")
    assert store._db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def test_an_old_database_is_converted_when_compacted(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)  # made before auto_vacuum was turned on
    db.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, messages TEXT)")
    db.executemany("INSERT INTO sessions VALUES (?,?)", [(str(i), "y" * 400_000) for i in range(20)])
    db.commit()
    db.execute("DELETE FROM sessions")
    db.commit()
    assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    assert sqlstore.compact(db) > 0
    assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert path.stat().st_size < 1_000_000


def test_compact_leaves_a_mostly_full_database_alone(tmp_path):
    db = sqlite3.connect(tmp_path / "full.db")
    db.execute("CREATE TABLE t (v TEXT)")
    db.executemany("INSERT INTO t VALUES (?)", [("z" * 1000,) for _ in range(2000)])
    db.commit()
    assert sqlstore.compact(db) == 0
