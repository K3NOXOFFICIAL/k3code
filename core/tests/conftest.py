"""Pytest configuration and fixtures."""

from __future__ import annotations

import contextlib
import os
import sys
import types
from pathlib import Path

import httpx
import pytest
import respx

# Captured at import, before any fixture patches HOME: this is the real home a test must never write to.
_REAL_HOME = Path(os.path.expanduser("~")).resolve()
_CHECKOUT = Path(__file__).resolve().parents[2]  # the repo / worktree root: pycache and .venv live there, so it is fine
_XDG_VARS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")
# uv keeps its download cache under the real home; tests that run uv need it (an empty cache breaks `uv lock --offline`)
_REAL_UV_CACHE = os.environ.get("UV_CACHE_DIR") or str(
    Path(os.environ.get("XDG_CACHE_HOME") or _REAL_HOME / ".cache") / "uv")
_REAL_ROOTS = tuple(sorted({str(_REAL_HOME), *(os.path.abspath(os.environ[v]) for v in
                                              ("K3CODE_HOME", "K3CODE_DATA", *_XDG_VARS)
                                              if os.path.isabs(os.environ.get(v, "")))}))
_ALLOWED_ROOTS: list[str] = [str(_CHECKOUT)]  # the session fixture adds pytest's basetemp (it may sit under HOME)
_HOME_WRITES: list[str] = []  # writes that reached the real home; counted and reported, never printed

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_PATH_WRITE_EVENTS = frozenset({
    "os.mkdir", "os.rename", "os.replace", "os.remove", "os.unlink", "os.rmdir", "os.symlink", "os.link",
    "os.chmod", "os.chown", "os.truncate", "os.utime", "shutil.rmtree", "sqlite3.connect",
})


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _audit(event: str, args: tuple) -> None:
    """Record (never block) any write under the real home that is outside tmp_path and the checkout.

    Reads the audit event only: it opens nothing under the real home, so it cannot disturb the user's files.
    """
    if event == "open":  # io.open passes (path, mode, flags); os.open passes (path, None, flags)
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
        candidates = args[:1] if writes else ()
    elif event in _PATH_WRITE_EVENTS:
        candidates = args[:2]
    else:
        return
    for raw in candidates:
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        elif isinstance(raw, os.PathLike):
            raw = os.fspath(raw)
        if not isinstance(raw, str) or not raw or raw == ":memory:":
            continue
        path = os.path.abspath(raw)
        if any(_under(path, ok) for ok in _ALLOWED_ROOTS):
            continue
        if any(_under(path, root) for root in _REAL_ROOTS):
            _HOME_WRITES.append(path)
            return


sys.addaudithook(_audit)


@pytest.fixture(scope="session", autouse=True)
def home_sentinel(tmp_path_factory):
    """Suite-level sentinel: every test's writes are checked against the real home (see ``_audit``)."""
    _ALLOWED_ROOTS.append(str(tmp_path_factory.getbasetemp()))
    yield types.SimpleNamespace(real_home=_REAL_HOME, writes=_HOME_WRITES)


def pytest_sessionfinish(session, exitstatus):
    if _HOME_WRITES:
        session.exitstatus = 1
        sys.stderr.write(f"\nHOME SENTINEL: {len(_HOME_WRITES)} write(s) reached the real home outside tmp_path "
                         "(paths withheld; see tests/conftest.py)\n")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """HOME and the XDG base dirs get a per-test temp dir of their own, so a write to ``~`` never reaches the real home.

    The dir sits beside ``tmp_path``, not inside it: the bash sandbox refuses a project dir that contains $HOME
    (the same reason ``k3home()`` in the tests sits beside the project dir).
    """
    base = tmp_path.parent / f"{tmp_path.name}-home"
    monkeypatch.setenv("HOME", str(base))
    for var in _XDG_VARS:
        monkeypatch.setenv(var, str(base / "xdg" / var.lower()))
    monkeypatch.setenv("UV_CACHE_DIR", _REAL_UV_CACHE)


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
def _no_real_tuios(monkeypatch):
    """Tests run inside k3 panes must not report to the real tuios daemon."""
    for var in ("TUIOS_SOCKET", "TUIOS_PANE_ID", "TUIOS_PANE_TOKEN", "TUIOS_SESSION"):
        monkeypatch.delenv(var, raising=False)


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
