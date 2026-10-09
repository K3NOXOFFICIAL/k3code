"""Daemon test helpers shared by test_daemon.py and test_session_sweep.py (a module of their own: no import cycle)."""

from __future__ import annotations

import asyncio
import io

import pytest


async def _stop_daemon(server, task: asyncio.Task, timeout: float = 10.0) -> None:
    """Stop a test daemon with a hard bound. ``wait_for(task, ...)`` is none: on timeout it cancels the task and then
    waits for the cancellation to finish, so a shutdown stuck in ``server.close()`` hung the run with no stack."""
    if server is None:  # never got as far as publishing its server: nothing to ask, just cancel
        task.cancel()
    else:
        server.request_stop()
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        stack = io.StringIO()
        task.print_stack(file=stack)
        task.cancel()
        pytest.fail(f"the daemon did not stop within {timeout:g} s; it was stuck in:\n{stack.getvalue()}")
    if server is not None:
        task.result()  # the daemon's own error, if it had one
