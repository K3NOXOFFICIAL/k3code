"""Deciding a bash call costs time linear in its simple commands, and it runs off the daemon's event loop.

K3NOX-691: the credential check looked every simple command up in every pipeline of the line, so a call of thousands
of simple commands (one heredoc fed to a shell is enough) took seconds to minutes in decide(), and decide() ran on the
event loop that serves every session, the socket and the heartbeats. The cost is now linear (a step count, not a
clock, pins it), and the loop awaits the bash decision in a worker thread.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from k3code.permissions import Decision, decide


def _bash(cmd: str, mode: str, cwd: Path) -> Decision:
    return decide(mode=mode, tool="bash", args={"command": cmd}, cwd=cwd)


def _lines(n: int) -> list[str]:
    # distinct text per line: the shlex cache (hardline._split_words) cannot make a long line cheap or a count vary
    return [f"echo hi > f691_{k}" for k in range(n)]


#: the spellings of one call of many simple commands: a heredoc a shell runs, newlines, the list operators, pipelines,
#: a launcher's quoted argument and a substitution (the last two are nested lines: their own Parsed children)
SPELLINGS: dict[str, Callable[[int], str]] = {
    "heredoc-to-shell": lambda n: "bash <<EOF\n" + "\n".join(_lines(n)) + "\nEOF\n",
    "newlines": lambda n: "\n".join(_lines(n)),
    "semicolons": lambda n: "; ".join(_lines(n)),
    "and-list": lambda n: " && ".join(_lines(n)),
    "pipelines": lambda n: "\n".join(f"echo {k} | wc -c" for k in range(n)),
    "bash-c": lambda n: 'bash -c "' + "; ".join(_lines(n)) + '"',
    "substitution": lambda n: "echo $(" + "; ".join(_lines(n)) + ")",
    # the hardline's xargs, wrapper/reserved-word and find -exec readings (each once grew with the square)
    "xargs-pipeline": lambda n: "echo a" + "".join(f" | xargs rm -rf f691_{k}" for k in range(n)),
    "time-braces": lambda n: "time { " * n + "echo hi" + " }" * n,
    "find-exec": lambda n: "find ~" + "".join(f" -exec echo f691_{k} {{}} \\;" for k in range(n)),
    # wrapper chains (older than the readings above: each wrapper or eval copied or re-split the rest of the words)
    "eval-chain": lambda n: "eval " * n + "echo hi",
    "sudo-chain": lambda n: "sudo " * n + "echo hi",
    # a relative cd walked every CDPATH entry on the line (each prefix assignment adds one)
    "cdpath-chain": lambda n: "; ".join(f"CDPATH=/a{k} cd b{k % 5}" for k in range(n)),
}


def _python_calls(cmd: str, cwd: Path) -> int:
    """How many Python calls and generator steps deciding ``cmd`` takes: the same for the same text on every host."""
    count = 0

    def profiler(frame, event, arg) -> None:  # noqa: ANN001
        nonlocal count
        if event == "call":
            count += 1

    sys.setprofile(profiler)
    try:
        _bash(cmd, "default", cwd)
    finally:
        sys.setprofile(None)
    return count


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_deciding_a_call_is_linear_in_its_simple_commands(tmp_path: Path, spelling: str) -> None:
    build = SPELLINGS[spelling]
    _bash(build(3), "default", tmp_path)  # warm the module caches (compiled regexes, mounts)
    small = _python_calls(build(300), tmp_path)
    large = _python_calls(build(1200), tmp_path)
    # four times the commands is four times the steps (measured: 3.97 to 4.00); Main took 4.4 to 5.4 times here, and
    # more the longer the line (subs x pipelines). A substitution's commands were never looked up: it stays as a guard.
    assert large <= 4.2 * small, (spelling, small, large)


@pytest.mark.serial  # wall-clock limit; load-sensitive under the 8-worker run, passes alone
def test_a_256_kb_heredoc_of_simple_commands_is_decided_in_seconds_not_minutes(tmp_path: Path) -> None:
    # 20,000 commands, 256 KB. Measured on kokadock: 1.3 s (Main: 29 s; 13 s for 13,000 commands).
    # The step count above pins the growth; this only catches a gross slowdown, so its limit leaves room for load.
    cmd = "bash <<EOF\n" + "echo hi > f1\n" * 20_000 + "EOF\n"
    assert len(cmd) > 256_000
    start = time.perf_counter()
    dec = _bash(cmd, "default", tmp_path)
    assert time.perf_counter() - start < 5.0
    assert dec.action == "ask"  # a program fed to a shell through a heredoc: no rule vouches for it


# whether a simple command feeds a pipe decides how a name-only command's argument counts: `stat .env` prints its
# metadata, `stat .env | x` hands the output on. Looking the commands up once per line must not change which ones count.
PIPED = [
    ("stat .env", False),
    ("stat .env | cat", True),
    ("echo a\n" * 500 + "stat .env | cat", True),
    ("stat .env | cat\n" + "echo a\n" * 500, True),
    ("echo a | cat\n" * 500 + "stat .env", False),
    ("stat .env; stat .env | cat", True),  # the same text piped elsewhere on the line counts for both
    ("bash <<EOF\n" + "echo a\n" * 500 + "stat .env | cat\nEOF\n", True),
]


@pytest.mark.parametrize(("cmd", "denied"), PIPED, ids=range(len(PIPED)))
def test_only_a_command_that_feeds_a_pipe_reads_its_name_only_arguments(tmp_path: Path, cmd: str, denied: bool) -> None:
    for mode in ("default", "auto", "yolo"):
        assert (_bash(cmd, mode, tmp_path).action == "deny") == denied, mode


async def test_a_slow_bash_decision_leaves_the_event_loop_to_the_other_sessions(tmp_path: Path, monkeypatch) -> None:
    from k3code.agent.loop import AgentLoop
    from k3code.providers.types import ToolCall

    class _R:
        chain: list = []

    loop = AgentLoop(_R(), system_prompt="x", cwd=tmp_path, permission_mode="default")  # type: ignore[arg-type]
    answered = threading.Event()

    def slow_decide(tool: str, args: dict, *, headless: bool = False) -> Decision:
        # another session's coroutine answers while this decision is made; on the event loop it never gets to run
        ran = answered.wait(timeout=3)
        return Decision(action="deny", message="the loop kept running" if ran else "the loop was blocked")

    monkeypatch.setattr(loop.permissions, "decide", slow_decide)

    async def other_session() -> None:
        await asyncio.sleep(0)
        answered.set()

    call = ToolCall(id="c1", name="bash", arguments={"command": "echo hi"})
    result, _ = await asyncio.gather(loop._execute_tool(call), other_session())
    assert result == {"error": "the loop kept running"}
