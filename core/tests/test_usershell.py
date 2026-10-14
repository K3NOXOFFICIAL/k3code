"""The runner of the user's own `!cmd` commands: output, exit code, timeout, cancel, the process-group kill (also when
the awaiting task itself is cancelled), stdin, the environment, passed descriptors, the process dock, streaming."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time

import pytest

from k3code.tools import jobs, usershell
from k3code.tools.usershell import ShellStreamer, run_user_command


def group_alive(pgid: int) -> list[int]:
    """Live (not zombie) processes of process group ``pgid``."""
    alive = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[2]) == pgid and fields[0] != "Z":
            alive.append(int(d))
    return alive


def pid_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


async def gone(pgid: int, within: float) -> bool:
    deadline = time.monotonic() + within
    while group_alive(pgid):
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def kill_leftovers(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


async def test_stdout_and_stderr_are_split_in_order_with_the_exit_code(tmp_path):
    seen: list[tuple[str, bytes]] = []
    run = await run_user_command(
        "echo a; sleep 0.1; echo b >&2; sleep 0.1; echo c; exit 3",
        cwd=tmp_path,
        on_output=lambda s, b: seen.append((s, b)),
    )
    assert (run.stdout, run.stderr, run.exit_code) == ("a\nc\n", "b\n", 3)
    assert seen == [("stdout", b"a\n"), ("stderr", b"b\n"), ("stdout", b"c\n")]
    assert not (run.timed_out or run.cancelled or run.overflowed or run.error)


async def test_large_output_is_clipped_and_a_flood_is_stopped(tmp_path, monkeypatch):
    run = await run_user_command("head -c 200000 /dev/zero | tr '\\0' x", cwd=tmp_path)
    assert run.exit_code == 0 and "truncated" in run.stdout and len(run.stdout) < 60_000
    monkeypatch.setattr(usershell, "USER_SHELL_MAX_BYTES", 100_000)
    run = await run_user_command("echo $$; yes", cwd=tmp_path, timeout=20)
    pgid = int(run.stdout.split()[0])
    assert run.overflowed and run.error == "output over 100000 bytes: stopped"
    assert await gone(pgid, 3)


async def test_a_timeout_keeps_the_partial_output_and_kills_the_group(tmp_path):
    started = time.monotonic()
    run = await run_user_command("echo $$; echo started; sleep 30", cwd=tmp_path, timeout=0.5)
    assert time.monotonic() - started < 5
    assert run.timed_out and not run.cancelled
    assert run.stdout.splitlines()[1] == "started"
    assert run.error == "timed out after 0.5 s (stdin is closed: interactive programs are not supported)"
    assert await gone(int(run.stdout.split()[0]), 3)


async def test_cancel_keeps_the_partial_output_and_leaves_no_survivor(tmp_path):
    """The children do not hold the pipes, so only a kill of the whole group stops them."""
    cancel = asyncio.Event()
    lines: list[bytes] = []

    def out(stream: str, chunk: bytes) -> None:
        lines.append(chunk)
        if b"".join(lines).count(b"\n") >= 2:
            cancel.set()

    cmd = "echo $$; sleep 30 >/dev/null 2>&1 & echo $!; sleep 31 >/dev/null 2>&1"
    run = await asyncio.wait_for(run_user_command(cmd, cwd=tmp_path, cancel=cancel, on_output=out), 10)
    pgid, child = map(int, run.stdout.split())
    try:
        assert run.cancelled and not run.timed_out and run.error == ""
        assert await gone(pgid, 3) and not pid_alive(child)
    finally:
        kill_leftovers(pgid)


async def test_cancelling_the_awaiting_task_still_kills_the_group_even_when_cancelled_again(tmp_path):
    """A disconnect cancels the handler, a shutdown may cancel it again while it kills: the kill is shielded, so the
    SIGKILL after the ignored SIGTERM still comes."""
    printed = asyncio.Event()
    out: list[bytes] = []

    def on_output(stream: str, chunk: bytes) -> None:
        out.append(chunk)
        printed.set()

    task = asyncio.ensure_future(
        run_user_command("trap '' TERM; echo $$; sleep 30 >/dev/null 2>&1", cwd=tmp_path, on_output=on_output)
    )
    await asyncio.wait_for(printed.wait(), 5)
    pgid = int(b"".join(out).split()[0])
    try:
        task.cancel()
        await asyncio.sleep(0.3)  # inside the kill now: SIGTERM is ignored, it waits to send SIGKILL
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert group_alive(pgid)  # still in its SIGTERM grace
        assert await gone(pgid, 6)
    finally:
        kill_leftovers(pgid)


async def test_stdin_is_closed_or_fed(tmp_path):
    started = time.monotonic()
    run = await run_user_command("read x; echo rc=$?", cwd=tmp_path, timeout=10)
    assert run.stdout == "rc=1\n" and time.monotonic() - started < 3
    assert (await run_user_command("cat", cwd=tmp_path, stdin=b"fed")).stdout == "fed"


async def test_extra_env_reaches_the_child_but_never_the_daemon_socket(tmp_path):
    extra = {"K3_ONE_SHOT": "/run/one-shot", "K3CODE_GATEWAY_SOCKET": "/run/gw.sock"}
    run = await run_user_command(
        'echo "$K3_ONE_SHOT|${K3CODE_GATEWAY_SOCKET:-none}|$PAGER"', cwd=tmp_path, extra_env=extra
    )
    assert run.stdout == "/run/one-shot|none|cat\n"


async def test_pass_fds_reach_the_child(tmp_path):
    r, w = os.pipe()
    try:
        run = await run_user_command(f"echo via-fd >&{w}", cwd=tmp_path, pass_fds=(w,))
        os.close(w)
        w = -1
        assert run.exit_code == 0 and os.read(r, 100) == b"via-fd\n"
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)


async def test_the_process_dock_shows_the_command_while_it_runs(tmp_path):
    task = asyncio.ensure_future(run_user_command("sleep 0.6", cwd=tmp_path, session_id="dock-s"))
    for _ in range(100):
        if jobs.REGISTRY.processes("dock-s"):
            break
        await asyncio.sleep(0.02)
    assert [p["command"] for p in jobs.REGISTRY.processes("dock-s")] == ["sleep 0.6"]
    await task
    assert jobs.REGISTRY.processes("dock-s") == []


async def test_a_command_that_cannot_start_says_so(tmp_path):
    run = await run_user_command("true", cwd=tmp_path / "missing")
    assert run.exit_code == 127 and run.error.startswith("could not start the command")


async def test_streaming_coalesces_small_writes(tmp_path):
    events: list[dict] = []
    streamer = ShellStreamer(events.append)
    pacer = asyncio.ensure_future(streamer.run())
    try:
        run = await run_user_command(
            "for i in $(seq 1 40); do echo line$i; sleep 0.02; done", cwd=tmp_path, on_output=streamer.feed
        )
    finally:
        pacer.cancel()
    streamer.finish()
    assert "".join(e["text"] for e in events) == run.stdout
    assert 2 <= len(events) <= 15  # 40 writes over ~1 s: about one event per 100 ms, not one per write


async def test_streaming_caps_each_event_and_the_total_then_goes_silent(tmp_path):
    events: list[dict] = []
    streamer = ShellStreamer(events.append)
    pacer = asyncio.ensure_future(streamer.run())
    try:
        await run_user_command("head -c 1048576 /dev/zero | tr '\\0' a", cwd=tmp_path, on_output=streamer.feed)
    finally:
        pacer.cancel()
    streamer.finish()
    texts = [e["text"] for e in events[:-1]]
    assert events[-1] == {"notice": ShellStreamer.NOTICE}
    assert all(len(t.encode()) <= 16 * 1024 for t in texts)
    assert sum(len(t) for t in texts) == 256 * 1024 and len(events) == 17
    streamer.feed("stdout", b"more")
    streamer.finish()
    assert len(events) == 17


@pytest.mark.parametrize("chunk", [b"\xc3\xa9" * 10_000, b"ok\xff" * 7000])
def test_streamed_text_is_cut_on_character_boundaries(chunk):
    events: list[dict] = []
    streamer = ShellStreamer(events.append)
    streamer.feed("stdout", chunk)
    streamer.finish()
    assert all(len(e["text"].encode()) <= 16 * 1024 for e in events)
    assert "".join(e["text"] for e in events) == chunk.decode("utf-8", errors="replace")
