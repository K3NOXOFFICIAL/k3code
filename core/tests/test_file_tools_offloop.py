"""read, edit and write run off the event loop: one session's big file no longer freezes every other session.

The gateway serves all sessions from one loop, so a synchronous handler body (a failed edit tries every strategy, then
a similarity search over every line) stalls streaming, approvals and every other turn until it returns."""

from __future__ import annotations

import threading

import pytest

import k3code.tools as tools
from k3code.tools import tool_edit, tool_read, tool_write


@pytest.mark.parametrize("name", ["read", "write", "edit"])
async def test_each_file_tool_body_runs_on_another_thread(tmp_path, monkeypatch, name):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\n")
    main = threading.get_ident()
    seen: list[int] = []
    real = tools._atomic_write if name != "read" else tools._split_lines

    def spy(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(tools, "_atomic_write" if name != "read" else "_split_lines", spy)
    calls = {
        "read": lambda: tool_read({"path": str(f)}, cwd=tmp_path),
        "write": lambda: tool_write({"path": str(f), "content": "x"}, cwd=tmp_path),
        "edit": lambda: tool_edit({"path": str(f), "old_string": "two", "new_string": "2"}, cwd=tmp_path),
    }
    result = await calls[name]()
    assert "error" not in result
    assert seen and main not in seen


async def test_a_failed_edit_on_a_big_file_does_not_stall_the_loop(tmp_path, monkeypatch):
    import asyncio

    f = tmp_path / "big.py"
    f.write_text("".join(f"    value_{i} = compute({i}) + helper(x, {i})\n" for i in range(20_000)))
    gaps: list[float] = []
    stop = False

    async def heartbeat() -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not stop:
            await asyncio.sleep(0.01)
            now = loop.time()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    result = await tool_edit(
        {"path": str(f), "old_string": "    value_7 = compute(8) + helper(y, 7)\n", "new_string": "x\n"}, cwd=tmp_path
    )
    stop = True
    await beat
    assert "error" in result
    assert len(gaps) > 5  # the loop kept ticking while the edit ran
    assert max(gaps) < 0.5, f"the loop was blocked for {max(gaps):.2f}s"


async def test_edit_refuses_a_file_over_the_read_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "MAX_READ_BYTES", 100)
    f = tmp_path / "big.log"
    f.write_text("".join(f"line{i}\n" for i in range(1, 51)))  # ~340 bytes
    result = await tool_edit({"path": str(f), "old_string": "line3", "new_string": "x"}, cwd=tmp_path)
    assert "(limit " in result["error"]
    assert "bash" in result["error"]
    assert f.read_text().startswith("line1\nline2\nline3\n")  # untouched
