"""``decide`` denies a command, path or cwd the OS cannot take, instead of raising.

A NUL byte cannot reach a shell argument or a path (the spawn and every file open fail), and a lone surrogate (a JSON
``"\\ud800"``) cannot be encoded for the OS. Path handling raised ``ValueError`` on both after a ``~`` (the user-name
lookup), which ended the turn, and a NUL inside a protected path or a hardline command broke the matching, so the
spelling got a weaker decision than the plain one. Every mode and tool now answers with a deny. A surrogate *escape*
(``\\udc80``, a file name the OS could not decode) encodes and stays decided like any other name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code.permissions import decide

MODES = ["default", "accept-edits", "auto", "yolo", "plan"]
PATH_TOOLS = ["read", "write", "edit", "glob", "grep"]
COMMANDS = [
    "echo hi > out.txt",
    "cat README.md",
    "rm -rf build",
    "rm -rf /",
    "git push --force origin main",
    "cat ~/.ssh/id_rsa",
    "cd sub && ls",
]
PATHS = ["notes.txt", "sub/a.py", "~/.ssh/id_rsa", ".env", "~/notes"]
UNUSABLE = ["\0", "\ud800"]


def _args(tool: str, value: str, key: str | None = None) -> dict[str, str]:
    if tool == "bash":
        return {"command": value} if key is None else {"command": "ls", key: value}
    return {key or "path": value}


@pytest.fixture
def cwd(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    return tmp_path


@pytest.mark.parametrize("bad", UNUSABLE)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("tool", "key"),
    [("bash", None), ("bash", "cwd"), *[(t, k) for t in PATH_TOOLS for k in ("path", "file")], ("glob", "pattern")],
)
def test_unusable_argument_is_denied_in_every_mode(cwd, bad, mode, tool, key):
    for prefix in ("", "~", "x", "~/"):
        dec = decide(mode=mode, tool=tool, args=_args(tool, prefix + bad + "/tail", key), cwd=cwd)
        assert dec.action == "deny", (prefix, dec)
        assert "NUL" in dec.message or "encoded" in dec.message


@pytest.mark.parametrize("bad", UNUSABLE)
@pytest.mark.parametrize("mode", MODES)
def test_unusable_byte_in_every_position_never_raises_and_always_denies(cwd, bad, mode):
    for command in COMMANDS:
        for i in range(len(command) + 1):
            dec = decide(mode=mode, tool="bash", args={"command": command[:i] + bad + command[i:]}, cwd=cwd)
            assert dec.action == "deny", (command, i)
    for tool in PATH_TOOLS:
        for path in PATHS:
            for i in range(len(path) + 1):
                dec = decide(mode=mode, tool=tool, args={"path": path[:i] + bad + path[i:]}, cwd=cwd)
                assert dec.action == "deny", (tool, path, i)


def test_a_surrogate_escape_stays_decided_like_any_name(cwd):
    """``\\udc80`` is how Python holds a file-name byte it could not decode: it encodes, so it is a real name."""
    if not _can_make("caf\udce9.txt", cwd):
        pytest.skip("this file system takes no undecodable name")
    dec = decide(mode="auto", tool="read", args={"path": "caf\udce9.txt"}, cwd=cwd)
    assert dec.action == "allow"
    dec = decide(mode="default", tool="bash", args={"command": "cat caf\udce9.txt"}, cwd=cwd)
    assert dec.action == "allow"


def _can_make(name: str, cwd: Path) -> bool:
    try:
        (cwd / name).write_bytes(b"x")
    except (OSError, UnicodeError):
        return False
    return True


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_content_with_a_nul_is_not_refused(cwd, tool):
    """Only what names a command or a path is checked: a file body or an edit string may hold any character."""
    args = {"path": "data.bin", "content": "a\0b", "old_string": "a\0", "new_string": "\0b"}
    dec = decide(mode="auto", tool=tool, args=args, cwd=cwd)
    assert dec.action != "deny" or "NUL" not in dec.message
