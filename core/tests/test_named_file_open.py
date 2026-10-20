"""``read`` and ``grep`` on a named file open the file the permission engine allowed (K3NOX-719).

The engine decides on a path; a tool that then opens that path by name opens whatever the name means by then. A
background job in the sandbox can swap a directory of the project for a link to a directory outside every root in that
gap, and the tool returned the outside file without a prompt. The tools now walk the path one component at a time, never
following a link they have not read, and ask the engine about the path they actually reached before they open its last
component and again on the open file.
"""

from __future__ import annotations

import ctypes
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from k3code.agent.loop import AgentLoop, ApprovalResult
from k3code.permissions import PermissionMode
from k3code.permissions.engine import decide
from k3code.providers.types import ToolCall
from k3code.tools import _grep_fallback, _read_sync
from k3code.tools.safe_open import NotAllowed, NotRegular, file_guard, open_regular

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc (Linux)")

#: built at run time so the secret scan never sees a key-shaped literal
SECRET = "not-a-real" + "-value-k719"
AT_FDCWD = -100
RENAME_EXCHANGE = 2
RACE_RUNS = 120


class _NoRouter:
    pass


def _engine_guard(proj: Path, tool: str = "read"):
    """What the agent loop installs for an allowed call: the engine's own answer for the path reached."""

    def guard(real: str) -> bool:
        return decide(mode=PermissionMode.AUTO, tool=tool, args={"path": real}, cwd=proj).action == "allow"

    return guard


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """``proj/cfg/f`` (inside the project), ``outside/f`` beside it and ``proj/cfg_swap`` linking to ``outside``."""
    outside, proj = tmp_path / "outside", tmp_path / "proj"
    outside.mkdir()
    (proj / "cfg").mkdir(parents=True)
    (outside / "f").write_text(f"TOKEN={SECRET}\n")
    (proj / "cfg" / "f").write_text("TOKEN in the repo\n")
    (proj / "cfg_swap").symlink_to(outside)
    return proj, outside


def _racer(proj: str, stop: threading.Event) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    a, b = f"{proj}/cfg".encode(), f"{proj}/cfg_swap".encode()
    while not stop.is_set():
        libc.renameat2(AT_FDCWD, a, AT_FDCWD, b, RENAME_EXCHANGE)


def _loop(proj: Path, mode: str = "auto") -> AgentLoop:
    async def deny(name: str, args: dict[str, Any], decision: Any) -> ApprovalResult:
        return ApprovalResult("deny")

    return AgentLoop(
        _NoRouter(),  # type: ignore[arg-type]
        system_prompt="x",
        cwd=proj,
        permission_mode=mode,
        headless=False,
        approval_callback=deny,
    )


async def _race(proj: Path, call: ToolCall) -> tuple[int, int]:
    """Run ``call`` through the loop while a racer swaps ``cfg``; (runs that returned the outside file, runs that
    returned the project's own file)."""
    loop = _loop(proj)
    stop = threading.Event()
    racer = threading.Thread(target=_racer, args=(str(proj), stop))
    racer.start()
    leaked = served = 0
    try:
        for i in range(RACE_RUNS):
            res = await loop._execute_tool(ToolCall(id=f"c{i}", name=call.name, arguments=dict(call.arguments)))
            text = str(res)
            leaked += SECRET in text
            served += "TOKEN in the repo" in text
    finally:
        stop.set()
        racer.join()
    return leaked, served


async def test_read_never_returns_the_outside_file_while_a_directory_is_swapped(tmp_path: Path) -> None:
    proj, _ = _tree(tmp_path)
    leaked, served = await _race(proj, ToolCall(id="x", name="read", arguments={"path": str(proj / "cfg" / "f")}))
    assert leaked == 0
    assert served  # the read itself still worked while the swap ran


async def test_grep_of_a_named_file_never_returns_the_outside_file_while_a_directory_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, _ = _tree(tmp_path)
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))  # no rg: the Python fallback runs
    call = ToolCall(id="x", name="grep", arguments={"pattern": "TOKEN", "path": str(proj / "cfg" / "f")})
    leaked, served = await _race(proj, call)
    assert leaked == 0
    assert served


def test_a_swap_that_lands_between_the_check_and_the_open_reads_the_directory_that_was_checked(tmp_path: Path) -> None:
    """The swap is made by the guard itself, at the instant after it answered: the file comes from the directory the
    walk already holds, not from the link that now has its name."""
    proj, _ = _tree(tmp_path)
    inner, swapped = _engine_guard(proj), []

    def swapping(real: str) -> bool:
        ok = inner(real)
        if not swapped:
            swapped.append(True)
            os.rename(proj / "cfg", proj / "cfg_real")
            os.rename(proj / "cfg_swap", proj / "cfg")  # cfg is now the link to the outside directory
        return ok

    with file_guard(swapping):
        res = _read_sync({"path": str(proj / "cfg" / "f")}, None)
    assert swapped
    assert res["content"] == "TOKEN in the repo"


def test_a_link_in_the_path_to_an_outside_file_is_refused_before_anything_there_is_opened(tmp_path: Path) -> None:
    proj, outside = _tree(tmp_path)
    os.rename(proj / "cfg", proj / "cfg_real")
    os.rename(proj / "cfg_swap", proj / "cfg")
    os.mkfifo(outside / "p")
    opened = threading.Event()

    def writer() -> None:
        fd = os.open(outside / "p", os.O_WRONLY)  # blocks until anything opens the FIFO for reading
        opened.set()
        os.close(fd)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    with file_guard(_engine_guard(proj)):
        for name in ("f", "p"):
            res = _read_sync({"path": str(proj / "cfg" / name)}, None)
            assert SECRET not in str(res)
            assert "error" in res
        with pytest.raises(NotAllowed):
            open_regular(proj / "cfg" / "p")
        res = _grep_fallback("TOKEN", proj / "cfg" / "f", None, None)
    assert SECRET not in res["matches"]
    assert "did not allow" in res["warnings"]
    t.join(0.5)
    released = opened.is_set()
    if not released:  # let the writer go so the thread does not outlive the test
        os.close(os.open(outside / "p", os.O_RDONLY | os.O_NONBLOCK))
        t.join(1)
    assert not released, "the FIFO outside the project was opened"


def test_ordinary_paths_read_as_before(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / "real" / "sub").mkdir(parents=True)
    (proj / "real" / "sub" / "f.txt").write_text("one\ntwo\nthree\n")
    (proj / "docs").symlink_to("real/sub")  # a relative link to a directory of the project
    (proj / "abs").symlink_to(proj / "real")  # an absolute one
    (proj / "alias.txt").symlink_to("real/sub/f.txt")  # a link to the file itself
    with file_guard(_engine_guard(proj)):
        for rel in ("real/sub/f.txt", "docs/f.txt", "abs/sub/f.txt", "alias.txt", "real/../real/sub/./f.txt"):
            res = _read_sync({"path": str(proj / rel)}, None)
            assert res["content"] == "one\ntwo\nthree", rel
            assert res["lines"] == "1-3 of 3"
        assert _read_sync({"path": str(proj / "real" / "sub" / "f.txt"), "offset": 2, "limit": 1}, None)["content"] == (
            "two"
        )
        assert "TOKEN" not in _grep_fallback("one", proj / "docs" / "f.txt", None, None)["matches"]
        assert "f.txt:1:one" in _grep_fallback("one", proj / "docs" / "f.txt", None, None)["matches"]


def test_a_missing_file_a_directory_and_a_fifo_are_not_found_and_nothing_blocks(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    os.mkfifo(tmp_path / "p")
    for name in ("nope.txt", "p", "d/nope/x"):
        res = _read_sync({"path": str(tmp_path / name)}, None)
        assert res == {"error": f"File not found: {tmp_path / name}"}
    # a directory is named as one, with what to use instead (the model used to retry a "missing" directory)
    res = _read_sync({"path": str(tmp_path / "d")}, None)
    assert res == {"error": f"{tmp_path / 'd'} is a directory; use glob to list it (read takes a file)"}
    with pytest.raises(NotRegular):
        open_regular(tmp_path / "p")
    with pytest.raises(FileNotFoundError):
        open_regular(tmp_path / "nope.txt")


def test_a_loop_of_links_is_an_error_not_a_hang(tmp_path: Path) -> None:
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    with pytest.raises(OSError) as exc:
        open_regular(tmp_path / "a")
    assert exc.value.errno == 40  # ELOOP
    assert _read_sync({"path": str(tmp_path / "a")}, None) == {"error": f"File not found: {tmp_path / 'a'}"}


def test_a_large_file_is_still_read_by_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import k3code.tools as tools

    monkeypatch.setattr(tools, "MAX_READ_BYTES", 64)
    big = tmp_path / "big.txt"
    big.write_text("".join(f"line {i}\n" for i in range(50)))
    assert "error" in _read_sync({"path": str(big)}, None)  # no range: told to give one
    res = _read_sync({"path": str(big), "offset": 3, "limit": 2}, None)
    assert res["content"] == "line 2\nline 3"
    assert res["lines"] == "3-4"


async def test_the_loop_installs_the_guard_for_read_and_grep_only(tmp_path: Path) -> None:
    proj, _ = _tree(tmp_path)
    loop = _loop(proj)
    allowed = loop.permissions.decide("read", {"path": str(proj / "cfg" / "f")})
    assert allowed.action == "allow"
    guard = loop._file_guard("read", {"path": str(proj / "cfg" / "f")}, allowed)
    assert guard is not None
    assert guard(str(proj / "cfg" / "f"))
    assert not guard(str(proj.parent / "outside" / "f"))  # a path outside the project is not what was allowed
    assert loop._file_guard("bash", {"command": "ls"}, allowed) is None
    # a person approved a read outside the project: it only has to stay clear of a refusal
    asking = _loop(proj, "default")
    asked = asking.permissions.decide("read", {"path": str(proj.parent / "outside" / "f")})
    assert asked.action == "ask"
    approved = asking._file_guard("read", {"path": str(proj.parent / "outside" / "f")}, asked)
    assert approved is not None
    assert approved(str(proj.parent / "outside" / "f"))
    assert not approved(os.path.expanduser("~/.ssh/id_ed25519"))  # a credential path stays refused
