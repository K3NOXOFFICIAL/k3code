"""The crash-resume transcript grows by appends: a save writes only what was added since the last one (#72)."""

from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

from k3code.providers.types import Message, ToolCall
from k3code.reliability.hooks import Reliability


def _rel(home: Path) -> Reliability:
    return Reliability.from_settings(None, session="s", home=home)


def _path(home: Path) -> Path:
    return home / "journal" / "s.messages.json"


def test_a_save_writes_only_the_new_messages(tmp_path: Path) -> None:
    # 200 × 50 KB tool results, saved after each one as the loop does. Every save used to serialise and rewrite the
    # whole conversation (tmp + replace) on the event loop: O(steps × transcript), 10 MB per save by the end.
    r = _rel(tmp_path)
    messages = [Message(role="system", content="sys"), Message(role="user", content="go")]
    r.save_transcript(messages)
    inode = _path(tmp_path).stat().st_ino
    grew, took = [], []
    for i in range(200):
        messages.append(Message(role="tool", content=f"{i:04d}" + "x" * 50_000, tool_call_id=f"c{i}", name="read"))
        before = _path(tmp_path).stat().st_size
        t0 = time.perf_counter()
        r.save_transcript(messages)
        took.append(time.perf_counter() - t0)
        grew.append(_path(tmp_path).stat().st_size - before)
        assert _path(tmp_path).stat().st_ino == inode  # appended to, never rewritten
    assert max(grew) < 51_000  # one ~50 KB line per save, however long the transcript already is
    assert statistics.median(took[-20:]) < 0.010  # the loop is not blocked by the transcript's size
    loaded = _rel(tmp_path).load_transcript()
    assert loaded is not None and [m.content for m in loaded] == [m.content for m in messages]


def test_a_crash_mid_append_loses_only_the_cut_message(tmp_path: Path) -> None:
    r = _rel(tmp_path)
    call = ToolCall(id="c1", name="bash", arguments={"command": "ls"})
    messages = [Message(role="user", content="hi"), Message(role="assistant", tool_calls=[call])]
    r.save_transcript(messages)
    with _path(tmp_path).open("a") as fh:
        fh.write('{"role": "tool", "content": "half a li')  # killed while appending the next message
    loaded = _rel(tmp_path).load_transcript()
    assert loaded is not None and len(loaded) == 2
    assert loaded[1].tool_calls[0].arguments == {"command": "ls"}


def test_the_file_is_rewritten_when_it_no_longer_matches(tmp_path: Path) -> None:
    r = _rel(tmp_path)
    a, b, c = (Message(role="user", content=x) for x in "abc")
    r.save_transcript([a, b])
    _path(tmp_path).unlink()  # cleared or swept underneath
    r.save_transcript([a, b, c])
    assert [m.content for m in _rel(tmp_path).load_transcript() or []] == ["a", "b", "c"]
    r.save_transcript([Message(role="user", content="resumed")])  # another list (resume, another loop)
    assert [m.content for m in _rel(tmp_path).load_transcript() or []] == ["resumed"]


def test_a_transcript_saved_as_one_json_array_still_loads(tmp_path: Path) -> None:
    # written by a version before the transcript was appended to: a crashed session must still resume
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('[{"role": "user", "content": "hi", "tool_calls": []}, {"role": "assistant", "content": "yo"}]')
    os.chmod(path, 0o600)
    assert [m.content for m in _rel(tmp_path).load_transcript() or []] == ["hi", "yo"]
