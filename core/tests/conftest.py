"""Pytest configuration and fixtures."""

from __future__ import annotations

import contextlib

import httpx
import pytest
import respx


@pytest.fixture
def mock_transport():
    """Provides a respx router for mocking HTTP calls."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def httpx_mock():
    """Alternative: httpx.MockTransport for lower-level control."""
    return httpx.MockTransport


@pytest.fixture(autouse=True)
def _isolated_k3code_home(tmp_path_factory, monkeypatch):
    """Never let tests write decisions/sessions into the real ~/.k3code."""
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path_factory.mktemp("k3home")))


@pytest.fixture(autouse=True)
def _no_real_nmcli(monkeypatch):
    """Netwatch must not spawn nmcli from tests (a cancel mid-spawn wedges loop teardown)."""
    from k3code.reliability import netwatch

    async def _none(timeout: float = 2.0) -> str | None:
        return None

    monkeypatch.setattr(netwatch, "nmcli_state", _none)


def _descendants(root: int) -> list[int]:
    """Live descendant pids of ``root`` (from /proc; Linux only, empty elsewhere)."""
    import os

    children: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(name))
    out: list[int] = []
    stack = [root]
    while stack:
        for pid in children.get(stack.pop(), []):
            out.append(pid)
            stack.append(pid)
    return out


@pytest.fixture(autouse=True, scope="session")
def _reap_child_processes():
    """Kill every process the suite spawned (MCP stdio servers, daemons, shells) at session end.

    A leaked child keeps pytest's stdout pipe open, so ``pytest | tail`` would never return.
    """
    yield
    import os
    import signal

    for pid in reversed(_descendants(os.getpid())):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
