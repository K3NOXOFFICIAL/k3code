"""Tool-call robustness corpus: realistic model mistakes fed through the loop's own validation and dispatch path.

Every case goes through ``AgentLoop._execute_tool`` (schema check, permission engine, read-before-edit guard,
handler), in a fresh tmp project, and its outcome is classified as data:

- ``ok``: the call did what the model plainly meant;
- ``repaired``: accepted after the arguments were normalised (see ``tools.validate``);
- ``actionable_error``: rejected with a message that names what to change (the case's ``hint`` regex matches what the
  model is sent, and it is no traceback). For a misnamed field that means naming the field as sent, not only the
  canonical one: "missing required 'path'" does not tell a model that sent ``file_path`` what was wrong;
- ``opaque_error``: rejected or crashed without saying how to fix it, or a success that did not do what was meant
  (the case's ``check`` fails).

Each case asserts its CURRENT class; ``known_gap=True`` marks the opaque ones. ``-k summary -s`` prints the counts.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from k3code import tools as tools_mod
from k3code.agent.loop import AgentLoop
from k3code.providers.types import ToolCall
from k3code.router import Router, build_chain
from k3code.tools import format_tool_result
from k3code.tools import validate as validate_mod

OK, REP, ACT, OPQ = "ok", "repaired", "actionable_error", "opaque_error"


@dataclass
class Case:
    id: str
    tool: str
    family: str
    args: Any
    expect: str
    hint: str = r"$^"  # what an actionable error must mention
    check: Any = None  # (project, model text) -> bool: the success did what was meant
    setup: Any = None  # (project, loop) -> None
    known_gap: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def has(*words: str):
    return lambda p, t: all(w in t for w in words)


def lacks(*words: str):
    return lambda p, t: not any(w in t for w in words)


def file_is(name: str, text: str):
    return lambda p, t: (p / name).exists() and (p / name).read_text() == text


def never(p: Path, t: str) -> bool:
    return False


def unread(name: str):
    def setup(p: Path, loop: AgentLoop) -> None:
        loop.file_stamps = type(loop.file_stamps)()

    return setup


A_PY = "def f():\n    return 1\n\n\ndef g():\n\treturn 'x'\n"
FILES = {
    "a.py": A_PY,
    "crlf.txt": "one\r\ntwo\r\n",
    "dup.txt": "x = 1\nx = 1\n",
    "quote.txt": 'say "hi"\n',
    "trail.txt": "value = 1   \nnext\n",
    "notes.md": "".join(f"line {i}\n" for i in range(1, 31)),
    "sub/b.py": "needle = 1\n",
    "sub/c.txt": "needle text\n",
    "sub/deep/d.py": "needle_deep = 2\n",
    "src/app.ts": "export const needle = 3;\n",
    "src/app.tsx": "export const x = 4;\n",
}
P = "<proj>"  # replaced by the project's absolute path


def _cases() -> list[Case]:
    c: list[Case] = []
    add = c.append
    path_alias = r"'(file_path|filepath|filename|file|dir|directory)'.*'path'"
    # ── read ──
    add(Case("read-ok", "read", "baseline", {"path": "a.py"}, OK, check=has("return 1")))
    for alias in ("file_path", "filepath", "filename", "file"):
        add(Case(f"read-alias-{alias}", "read", "near-miss name", {alias: "a.py"}, REP, path_alias))
    add(
        Case(
            "read-alias-and-path",
            "read",
            "near-miss name",
            {"path": "a.py", "file_path": "a.py"},
            ACT,
            path_alias,
            check=has("return 1"),
        )
    )
    add(Case("read-offset-str", "read", "wrong type", {"path": "notes.md", "offset": "5"}, REP, r"'offset'.*integer"))
    add(Case("read-limit-str", "read", "wrong type", {"path": "notes.md", "limit": "3"}, REP, r"'limit'.*integer"))
    add(
        Case(
            "read-offset-float",
            "read",
            "wrong type",
            {"path": "notes.md", "offset": 5.0, "limit": 1},
            OK,
            check=has("line 5"),
        )
    )
    add(Case("read-path-null", "read", "wrong type", {"path": None}, ACT, r"'path'.*string"))
    add(Case("read-path-list", "read", "wrong type", {"path": ["a.py"]}, ACT, r"'path'.*string"))
    add(Case("read-missing", "read", "missing field", {}, ACT, r"missing required 'path'"))
    add(Case("read-args-str", "read", "wrong type", "a.py", ACT, r"object"))
    add(
        Case(
            "read-extra",
            "read",
            "unknown field",
            {"path": "a.py", "encoding": "utf-8"},
            ACT,
            r"'encoding'.*'path'",
            check=has("return 1"),
        )
    )
    add(Case("read-abs", "read", "path form", {"path": f"{P}/a.py"}, OK, check=has("return 1")))
    add(Case("read-dot-slash", "read", "path form", {"path": "./a.py"}, OK, check=has("return 1")))
    add(Case("read-trailing-space", "read", "path form", {"path": "a.py "}, REP, r"whitespace|trailing"))
    add(Case("read-trailing-newline", "read", "path form", {"path": "a.py\n"}, REP, r"whitespace|newline"))
    add(Case("read-quoted", "read", "path form", {"path": '"a.py"'}, REP, r"quote"))
    add(Case("read-file-url", "read", "path form", {"path": f"file://{P}/a.py"}, REP, r"file://"))
    add(Case("read-dir", "read", "dir vs file", {"path": "sub"}, ACT, r"directory.*(glob|bash|ls)"))
    add(
        Case(
            "read-nonexistent",
            "read",
            "nonexistent",
            {"path": "nope.py"},
            OPQ,
            r"(glob|did you mean|no such)",
            known_gap=True,
        )
    )
    add(Case("read-near-name", "read", "nonexistent", {"path": "sub/b.pyy"}, OPQ, r"b\.py\b", known_gap=True))
    add(
        Case(
            "read-offset-0",
            "read",
            "offset/limit",
            {"path": "notes.md", "offset": 0, "limit": 2},
            OK,
            check=has("line 1", "line 2"),
        )
    )
    add(
        Case(
            "read-offset-past",
            "read",
            "offset/limit",
            {"path": "notes.md", "offset": 99},
            OK,
            check=has("past the end"),
        )
    )
    add(Case("read-limit-neg", "read", "offset/limit", {"path": "notes.md", "limit": -1}, OK, check=has("line 1")))
    add(
        Case(
            "read-start-end",
            "read",
            "offset/limit",
            {"path": "notes.md", "start": 3, "end": 4},
            OK,
            check=has("line 3", "line 4"),
        )
    )
    add(Case("read-binary", "read", "binary/huge", {"path": "bin.dat"}, ACT, r"binary.*(xxd|strings)"))
    add(Case("read-huge", "read", "binary/huge", {"path": "big.log"}, ACT, r"offset and limit"))
    add(
        Case(
            "read-huge-range",
            "read",
            "binary/huge",
            {"path": "big.log", "offset": 2, "limit": 2},
            OK,
            check=has("row 2"),
        )
    )
    add(Case("read-tilde", "read", "path form", {"path": "~/h.txt"}, REP, check=has("home file")))
    # ── write ──
    add(
        Case("write-new", "write", "baseline", {"path": "new.txt", "content": "hi"}, OK, check=file_is("new.txt", "hi"))
    )
    for alias in ("file_path", "filepath", "filename"):
        add(
            Case(
                f"write-alias-{alias}",
                "write",
                "near-miss name",
                {alias: "new.txt", "content": "hi"},
                REP,
                path_alias,
            )
        )
    add(
        Case(
            "write-alias-text",
            "write",
            "near-miss name",
            {"path": "new.txt", "text": "hi"},
            ACT,
            r"'text'.*'content'",
        )
    )
    add(
        Case(
            "write-alias-contents",
            "write",
            "near-miss name",
            {"path": "new.txt", "contents": "hi"},
            ACT,
            r"'contents'.*'content'",
        )
    )
    add(Case("write-content-int", "write", "wrong type", {"path": "n.txt", "content": 5}, ACT, r"'content'.*string"))
    add(
        Case("write-content-null", "write", "wrong type", {"path": "n.txt", "content": None}, ACT, r"'content'.*string")
    )
    add(
        Case(
            "write-content-list",
            "write",
            "wrong type",
            {"path": "n.txt", "content": ["a", "b"]},
            ACT,
            r"'content'.*string",
        )
    )
    add(Case("write-missing-content", "write", "missing field", {"path": "n.txt"}, ACT, r"missing required 'content'"))
    add(
        Case(
            "write-existing-unread",
            "write",
            "read-before-edit",
            {"path": "a.py", "content": "x"},
            ACT,
            r"read it before",
            setup=unread("a.py"),
        )
    )
    add(
        Case(
            "write-existing-read", "write", "baseline", {"path": "a.py", "content": "x"}, OK, check=file_is("a.py", "x")
        )
    )
    add(Case("write-to-dir", "write", "dir vs file", {"path": "sub", "content": "x"}, ACT, r"directory"))
    add(
        Case(
            "write-trailing-space",
            "write",
            "path form",
            {"path": "out.txt ", "content": "x"},
            REP,
            r"whitespace|trailing",
            check=file_is("out.txt", "x"),
        )
    )
    add(
        Case(
            "write-file-url",
            "write",
            "path form",
            {"path": f"file://{P}/out.txt", "content": "x"},
            REP,
            r"file://",
            check=file_is("out.txt", "x"),
        )
    )
    add(
        Case(
            "write-nested",
            "write",
            "path form",
            {"path": "x/y/z.txt", "content": "x"},
            OK,
            check=file_is("x/y/z.txt", "x"),
        )
    )
    add(
        Case(
            "write-extra",
            "write",
            "unknown field",
            {"path": "e.txt", "content": "x", "mode": "w"},
            ACT,
            r"'mode'.*'content'",
            check=file_is("e.txt", "x"),
        )
    )

    # ── edit ──
    def edit(**kw):
        return {"path": "a.py", **kw}

    add(
        Case(
            "edit-ok",
            "edit",
            "baseline",
            edit(old_string="return 1", new_string="return 2"),
            OK,
            check=lambda p, t: "return 2" in (p / "a.py").read_text(),
        )
    )
    for o, n in (
        ("old", "new"),
        ("old_str", "new_str"),
        ("oldText", "newText"),
        ("old_text", "new_text"),
        ("search", "replace"),
    ):
        add(
            Case(
                f"edit-alias-{o}",
                "edit",
                "near-miss name",
                edit(**{o: "return 1", n: "return 2"}),
                REP,
                rf"'{o}'.*'old_string'",
            )
        )
    add(
        Case(
            "edit-alias-file_path",
            "edit",
            "near-miss name",
            {"file_path": "a.py", "old_string": "return 1", "new_string": "return 2"},
            REP,
            path_alias,
        )
    )
    add(
        Case(
            "edit-missing-new",
            "edit",
            "missing field",
            edit(old_string="return 1"),
            ACT,
            r"missing required 'new_string'",
        )
    )
    add(
        Case(
            "edit-replace-all-str",
            "edit",
            "wrong type",
            {"path": "dup.txt", "old_string": "x = 1", "new_string": "y", "replace_all": "true"},
            REP,
            r"'replace_all'.*boolean",
        )
    )
    add(
        Case(
            "edit-new-null",
            "edit",
            "wrong type",
            edit(old_string="return 1", new_string=None),
            ACT,
            r"'new_string'.*string",
        )
    )
    add(
        Case(
            "edit-indent",
            "edit",
            "edit match",
            edit(old_string="  return 1", new_string="  return 2"),
            OK,
            check=lambda p, t: "return 2" in (p / "a.py").read_text(),
        )
    )
    add(
        Case(
            "edit-tab-vs-space",
            "edit",
            "edit match",
            edit(old_string="    return 'x'", new_string="    return 'y'"),
            OK,
            check=lambda p, t: "return 'y'" in (p / "a.py").read_text(),
        )
    )
    add(
        Case(
            "edit-crlf",
            "edit",
            "edit match",
            {"path": "crlf.txt", "old_string": "one\ntwo", "new_string": "1\n2"},
            OK,
            check=lambda p, t: (p / "crlf.txt").read_bytes() == b"1\r\n2\r\n",
        )
    )
    add(
        Case(
            "edit-trailing-space",
            "edit",
            "edit match",
            {"path": "trail.txt", "old_string": "value = 1\nnext", "new_string": "value = 2\nnext"},
            OK,
            check=lambda p, t: "value = 2" in (p / "trail.txt").read_text(),
        )
    )
    add(
        Case(
            "edit-smart-quotes",
            "edit",
            "edit match",
            {"path": "quote.txt", "old_string": "say “hi”", "new_string": 'say "yo"'},
            OK,
            check=lambda p, t: "yo" in (p / "quote.txt").read_text(),
        )
    )
    add(
        Case(
            "edit-not-found",
            "edit",
            "edit match",
            edit(old_string="zz_absent_token", new_string="x"),
            ACT,
            r"(not found|Could not find|no match).*",
        )
    )
    add(
        Case(
            "edit-not-unique",
            "edit",
            "edit match",
            {"path": "dup.txt", "old_string": "x = 1", "new_string": "y"},
            ACT,
            r"replace_all|unique|more context",
        )
    )
    add(
        Case(
            "edit-empty-old",
            "edit",
            "edit match",
            edit(old_string="", new_string="x"),
            ACT,
            r"old_string.*(empty|write)",
        )
    )
    add(
        Case(
            "edit-same",
            "edit",
            "edit match",
            edit(old_string="return 1", new_string="return 1"),
            ACT,
            r"(identical|same|no change)",
        )
    )
    add(
        Case(
            "edit-unread",
            "edit",
            "read-before-edit",
            edit(old_string="return 1", new_string="return 2"),
            ACT,
            r"read it before",
            setup=unread("a.py"),
        )
    )
    add(
        Case(
            "edit-nonexistent",
            "edit",
            "nonexistent",
            {"path": "nope.py", "old_string": "a", "new_string": "b"},
            ACT,
            r"(write|create|glob|did you mean)",
        )
    )
    add(
        Case(
            "edit-dir",
            "edit",
            "dir vs file",
            {"path": "sub", "old_string": "a", "new_string": "b"},
            ACT,
            r"directory",
        )
    )
    add(
        Case(
            "edit-binary",
            "edit",
            "binary/huge",
            {"path": "bin.dat", "old_string": "a", "new_string": "b"},
            OK,  # NUL bytes are valid UTF-8: the edit does what was asked
        )
    )
    add(
        Case(
            "edit-trailing-newline-path",
            "edit",
            "path form",
            {"path": "a.py\n", "old_string": "return 1", "new_string": "return 2"},
            REP,
            r"whitespace|newline",
            check=lambda p, t: "return 2" in (p / "a.py").read_text(),
        )
    )
    add(
        Case(
            "edit-line-numbers",
            "edit",
            "edit match",
            edit(old_string="     2\t    return 1", new_string="    return 2"),
            OPQ,
            r"line number",
            known_gap=True,
        )
    )
    # ── grep ──
    add(Case("grep-ok", "grep", "baseline", {"pattern": "needle"}, OK, check=has("b.py", "c.txt")))
    for alias in ("regex", "query", "search"):
        add(
            Case(
                f"grep-alias-{alias}",
                "grep",
                "near-miss name",
                {alias: "needle"},
                REP,
                rf"'{alias}'.*'pattern'",
            )
        )
    for alias in ("dir", "directory"):
        add(
            Case(
                f"grep-path-alias-{alias}",
                "grep",
                "near-miss name",
                {"pattern": "needle", alias: "src"},
                REP,
                path_alias,
                check=lacks("b.py"),
            )
        )
    add(
        Case(
            "grep-include-pattern",
            "grep",
            "near-miss name",
            {"pattern": "needle", "include_pattern": "*.py"},
            REP,
            r"'include_pattern'.*'include'",
            check=lacks("c.txt", "app.ts"),
        )
    )
    add(
        Case(
            "grep-glob-alias",
            "grep",
            "near-miss name",
            {"pattern": "needle", "glob": "*.py"},
            REP,
            r"'glob'.*'include'",
            check=lacks("c.txt", "app.ts"),
        )
    )
    add(
        Case(
            "grep-include-list",
            "grep",
            "baseline",
            {"pattern": "needle", "include": ["*.py"]},
            OK,
            check=lacks("c.txt", "app.ts"),
        )
    )
    add(
        Case(
            "grep-meta-paren",
            "grep",
            "regex",
            {"pattern": "f("},
            ACT,
            hint=r"(regex|escape|parenthesis|unterminated)",
        )
    )
    add(Case("grep-meta-dot", "grep", "regex", {"pattern": "app.ts"}, OK))
    add(
        Case(
            "grep-invalid",
            "grep",
            "regex",
            {"pattern": "[a-"},
            ACT,
            hint=r"(regex|escape|bracket|range|unterminated)",
        )
    )
    add(Case("grep-empty", "grep", "regex", {"pattern": ""}, ACT, r"pattern.*empty", check=never))
    add(Case("grep-pattern-null", "grep", "wrong type", {"pattern": None}, ACT, r"'pattern'.*string"))
    add(Case("grep-pattern-list", "grep", "wrong type", {"pattern": ["a", "b"]}, ACT, r"'pattern'.*string"))
    add(Case("grep-missing", "grep", "missing field", {"path": "."}, ACT, r"missing required 'pattern'"))
    add(Case("grep-file-path", "grep", "path form", {"pattern": "needle", "path": "sub/b.py"}, OK, check=has("1:")))
    add(
        Case(
            "grep-nonexistent",
            "grep",
            "nonexistent",
            {"pattern": "needle", "path": "nope"},
            ACT,
            r"(not found|does not exist|no such)",
            check=never,
        )
    )
    add(
        Case(
            "grep-outside",
            "grep",
            "path outside roots",
            {"pattern": "root", "path": "/etc/hostname"},
            ACT,
            r"(add-dir|ask the user)",  # "Outside project roots" names no way forward
            extra={"mode": "default"},
        )
    )
    add(
        Case(
            "grep-tilde",
            "grep",
            "path form",
            {"pattern": "home", "path": "~/h.txt"},
            ACT,
            r"(add-dir|ask the user)",
            extra={"mode": "default"},
        )
    )
    add(
        Case(
            "grep-case-flag",
            "grep",
            "unknown field",
            {"pattern": "NEEDLE", "-i": True},
            ACT,
            r"-i",
            check=has("b.py"),
        )
    )
    # ── glob ──
    add(Case("glob-ok", "glob", "baseline", {"pattern": "**/*.py"}, OK, check=has("sub/b.py", "a.py")))
    add(Case("glob-no-starstar", "glob", "glob form", {"pattern": "*.py"}, OK, check=has("sub/b.py", "a.py")))
    add(Case("glob-leading-slash", "glob", "glob form", {"pattern": "/sub/*.py"}, OK, check=has("b.py")))
    add(
        Case(
            "glob-abs-pattern",
            "glob",
            "glob form",
            {"pattern": f"{P}/sub/*.py"},
            OPQ,
            r"(absolute|path)",
            check=has("b.py"),
            known_gap=True,
        )
    )
    add(
        Case(
            "glob-braces",
            "glob",
            "glob form",
            {"pattern": "src/*.{ts,tsx}"},
            OPQ,
            r"brace",
            check=has("app.ts", "app.tsx"),
            known_gap=True,
        )
    )
    add(
        Case(
            "glob-dir-alias",
            "glob",
            "near-miss name",
            {"pattern": "*.py", "directory": "sub"},
            REP,
            path_alias,
            check=lacks("a.py"),
        )
    )
    add(Case("glob-alias-glob", "glob", "near-miss name", {"glob": "*.py"}, ACT, r"'glob'.*'pattern'"))
    add(
        Case(
            "glob-nonexistent",
            "glob",
            "nonexistent",
            {"pattern": "*.py", "path": "nope"},
            ACT,
            r"(not found|does not exist|no such)",
            check=never,
        )
    )
    add(
        Case(
            "glob-path-file",
            "glob",
            "dir vs file",
            {"pattern": "*.py", "path": "a.py"},
            ACT,
            r"directory",
            check=never,
        )
    )
    add(Case("glob-missing", "glob", "missing field", {}, ACT, r"missing required 'pattern'"))
    # ── bash ──
    add(Case("bash-ok", "bash", "baseline", {"command": "echo hi"}, OK, check=has("hi")))
    for alias in ("cmd", "script", "shell"):
        add(
            Case(
                f"bash-alias-{alias}",
                "bash",
                "near-miss name",
                {alias: "echo hi"},
                REP,
                rf"'{alias}'.*'command'",
            )
        )
    add(Case("bash-missing", "bash", "missing field", {}, ACT, r"missing required 'command'"))
    add(Case("bash-command-list", "bash", "wrong type", {"command": ["echo", "hi"]}, ACT, r"'command'.*string"))
    add(Case("bash-multiline", "bash", "bash form", {"command": "echo a\necho b"}, OK, check=has("a", "b")))
    add(Case("bash-trailing-amp", "bash", "bash form", {"command": "sleep 3 &", "timeout": 1}, ACT, r"background"))
    add(
        Case(
            "bash-timeout-str", "bash", "wrong type", {"command": "echo hi", "timeout": "5"}, REP, r"'timeout'.*number"
        )
    )
    add(Case("bash-timeout-ms", "bash", "wrong type", {"command": "echo hi", "timeout": 120000}, OK, check=has("hi")))
    add(
        Case(
            "bash-cwd-missing",
            "bash",
            "bash cwd",
            {"command": "pwd", "cwd": "nope"},
            ACT,
            r"cwd.*(exist|found)",
        )
    )
    add(
        Case(
            "bash-cwd-file",
            "bash",
            "bash cwd",
            {"command": "pwd", "cwd": "a.py"},
            ACT,
            r"cwd.*director",
        )
    )
    add(Case("bash-cwd-ok", "bash", "bash cwd", {"command": "pwd", "cwd": "sub"}, OK, check=has("/sub")))
    add(
        Case(
            "bash-background-str",
            "bash",
            "wrong type",
            {"command": "echo hi", "background": "true"},
            REP,
            r"'background'.*boolean",
        )
    )
    add(
        Case(
            "bash-run-in-background",
            "bash",
            "near-miss name",
            {"command": "echo hi", "run_in_background": True},
            ACT,
            r"'run_in_background'.*'background'",
            check=has("hi"),
        )
    )
    add(
        Case(
            "bash-description-extra",
            "bash",
            "unknown field",
            {"command": "echo hi", "description": "say hi"},
            ACT,
            r"'description'.*'command'",
            check=has("hi"),
        )
    )
    add(Case("bash-empty", "bash", "missing field", {"command": ""}, ACT, r"command.*empty", check=never))
    # ── bash_output / bash_kill ──
    add(Case("bash_output-unknown", "bash_output", "nonexistent", {"job_id": "job-9"}, ACT, r"no job .*\(jobs:"))
    add(Case("bash_output-int", "bash_output", "wrong type", {"job_id": 3}, ACT, r"'job_id'.*string"))
    add(
        Case(
            "bash_output-alias-id",
            "bash_output",
            "near-miss name",
            {"id": "job-1"},
            ACT,
            r"'id'.*'job_id'",
        )
    )
    add(
        Case(
            "bash_output-alias-shell_id",
            "bash_output",
            "near-miss name",
            {"shell_id": "job-1"},
            ACT,
            r"'shell_id'.*'job_id'",
        )
    )
    add(Case("bash_kill-unknown", "bash_kill", "nonexistent", {"job_id": "job-9"}, ACT, r"no job .*\(jobs:"))
    add(Case("bash_kill-alias-pid", "bash_kill", "near-miss name", {"pid": 123}, ACT, r"'pid'.*'job_id'"))
    # ── todo ──
    td = [{"content": "a", "status": "pending"}]
    add(Case("todo-ok", "todo", "baseline", {"todos": td}, OK, check=has("[ ] a")))
    add(Case("todo-items-alias", "todo", "near-miss name", {"items": td}, REP, r"'items'.*'todos'"))
    add(
        Case(
            "todo-json-string",
            "todo",
            "wrong type",
            {"todos": '[{"content": "a", "status": "pending"}]'},
            ACT,
            r"'todos'.*array",
        )
    )
    add(
        Case(
            "todo-bad-status",
            "todo",
            "wrong value",
            {"todos": [{"content": "a", "status": "done"}]},
            ACT,
            r"pending, in_progress, completed",
        )
    )
    add(
        Case(
            "todo-missing-status",
            "todo",
            "missing field",
            {"todos": [{"content": "a"}]},
            ACT,
            r"pending, in_progress, completed",
        )
    )
    add(
        Case(
            "todo-text-alias",
            "todo",
            "near-miss name",
            {"todos": [{"text": "a", "status": "pending"}]},
            ACT,
            r"'text'.*'content'",
        )
    )
    add(
        Case(
            "todo-two-in-progress",
            "todo",
            "wrong value",
            {"todos": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]},
            ACT,
            r"one todo may be in_progress",
        )
    )
    # ── web ──
    add(Case("web_fetch-ok", "web_fetch", "baseline", {"url": "https://example.com/doc"}, OK, check=has("hello web")))
    add(
        Case(
            "web_fetch-no-scheme",
            "web_fetch",
            "url form",
            {"url": "example.com/doc"},
            ACT,
            r"https?://",
        )
    )
    add(
        Case(
            "web_fetch-alias-uri",
            "web_fetch",
            "near-miss name",
            {"uri": "https://example.com/doc"},
            ACT,
            r"'uri'.*'url'",
        )
    )
    add(
        Case(
            "web_fetch-url-list", "web_fetch", "wrong type", {"url": ["https://example.com/doc"]}, ACT, r"'url'.*string"
        )
    )
    add(
        Case(
            "web_fetch-trailing-space",
            "web_fetch",
            "url form",
            {"url": " https://example.com/doc \n"},
            REP,
            r"whitespace",
            check=has("hello web"),
        )
    )
    add(Case("web_search-ok", "web_search", "baseline", {"query": "k3code"}, OK, check=has("Result for k3code")))
    add(
        Case(
            "web_search-limit-str",
            "web_search",
            "wrong type",
            {"query": "k3code", "limit": "3"},
            REP,
            r"'limit'.*integer",
        )
    )
    add(Case("web_search-alias-q", "web_search", "near-miss name", {"q": "k3code"}, REP, r"'q'.*'query'"))
    add(
        Case(
            "web_search-empty",
            "web_search",
            "missing field",
            {"query": "  "},
            ACT,
            r"query.*(empty|non-empty)",
        )
    )
    # ── skill ──
    add(Case("skill-unknown", "skill", "nonexistent", {"name": "nope-skill"}, ACT, r"Unknown skill"))
    add(Case("skill-empty", "skill", "missing field", {}, ACT, r"`name`.*`query`"))
    add(Case("skill-name-int", "skill", "wrong type", {"name": 5}, ACT, r"'name'.*string"))
    add(
        Case(
            "skill-alias-skill",
            "skill",
            "near-miss name",
            {"skill": "nope-skill"},
            ACT,
            r"'skill'.*'name'",
        )
    )
    # ── task ──
    add(Case("task-ok", "task", "baseline", {"description": "look", "prompt": "find x"}, OK, check=has("done: find x")))
    add(
        Case(
            "task-missing-description",
            "task",
            "missing field",
            {"prompt": "find x"},
            ACT,
            r"missing required 'description'",
        )
    )
    add(
        Case(
            "task-bad-tier",
            "task",
            "wrong value",
            {"description": "d", "prompt": "p", "tier": "large"},
            ACT,
            r"'tier' must be one of",
        )
    )
    add(
        Case(
            "task-background-str",
            "task",
            "wrong type",
            {"description": "d", "prompt": "p", "background": "false"},
            REP,
            r"'background'.*boolean",
        )
    )
    add(
        Case(
            "task-alias-task",
            "task",
            "near-miss name",
            {"description": "d", "task": "find x"},
            REP,
            r"'task'.*'prompt'",
        )
    )
    return c


CASES = _cases()


class _NoProvider:
    name = "fake"
    base_url = "https://fake.test"

    async def stream(self, *a, **kw):  # pragma: no cover - never called
        if False:
            yield None

    async def aclose(self) -> None:
        pass


class _Handle:
    def __init__(self, prompt: str) -> None:
        self.id, self.prompt, self.done = "sa-1", prompt, True

    def render(self) -> str:
        return f"done: {self.prompt}"


class _Subagents:
    handles: dict[str, Any] = {}

    def spawn(self, parent: Any, **kw: Any) -> _Handle:
        return _Handle(kw["prompt"])

    async def wait(self, h: _Handle) -> None:
        return None


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for name, text in FILES.items():
        (proj / name).parent.mkdir(parents=True, exist_ok=True)
        (proj / name).write_bytes(text.encode())
    (proj / "bin.dat").write_bytes(b"\x00\x01binary\x00")
    (proj / "big.log").write_text("".join(f"row {i}\n" for i in range(1, 2000)))
    home = Path.home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "h.txt").write_text("home file\n")
    return proj


def _loop(proj: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "yolo", **kw: Any) -> AgentLoop:
    from k3code.extratools import register_skill_tool
    from k3code.research import tools as research_tools
    from k3code.research.fetch import WebFetcher
    from k3code.subagents.tools import register_task_tools

    monkeypatch.setattr(tools_mod, "MAX_READ_BYTES", 4096)  # big.log counts as huge
    loop = AgentLoop(
        Router(build_chain([_NoProvider()], [["m"]]), max_retries=0),
        system_prompt="t",
        permission_mode=mode,
        cwd=proj,
        **kw,
    )
    loop._sandbox_argv = lambda **_: None  # about the arguments, not bwrap
    loop._home_rooted = lambda: False
    loop._bwrap_unusable = lambda: False
    page = "<html><head><title>Doc</title></head><body><p>hello web</p></body></html>"
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, html=page)))

    async def fake_search(query: str, n: int, **kw: Any):
        return [research_tools.Hit(f"Result for {query}", "https://example.com/r", "snippet")], [], True

    monkeypatch.setattr(research_tools, "fallback_search", fake_search)
    research_tools.register_web_tools(
        loop.tools,
        SimpleNamespace(research={"keyless_fallback": False}),
        fetcher=WebFetcher(client=client, respect_robots=False),
    )
    register_skill_tool(loop.tools, proj, [])
    register_task_tools(loop.tools, SimpleNamespace(subagents=_Subagents()), SimpleNamespace(session_id="s"))
    for name in [*FILES, "bin.dat", "big.log"]:
        loop.file_stamps.record(proj / name)
    return loop


def _subst(value: Any, proj: Path) -> Any:
    if isinstance(value, str):
        return value.replace(P, str(proj))
    if isinstance(value, dict):
        return {k: _subst(v, proj) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, proj) for v in value]
    return value


def _repaired(loop: AgentLoop, tool: str, args: Any) -> bool:
    normalise = getattr(validate_mod, "normalize_arguments", None)
    if normalise is None:
        return False
    spec = loop.tools.get(tool)[0]
    return normalise(tool, spec.parameters, args).args != args


_UNHELPFUL = re.compile(r"Traceback|Tool execution failed|^Error: ?$")


def classify(case: Case, proj: Path, loop: AgentLoop, args: Any, result: dict[str, Any]) -> tuple[str, str]:
    text = str(format_tool_result(result))
    seen = text.replace(str(proj), P).replace(str(Path.home()), "<home>")  # hints never match the tmp path
    failed = "error" in result and "content" not in result and "stdout" not in result
    if failed or (result.get("exit_code") not in (0, None)):
        if re.search(case.hint, seen, re.S) and not _UNHELPFUL.search(text):
            return ACT, text
        return OPQ, text
    if case.check is not None and not case.check(proj, text):
        return OPQ, text  # a wrong-looking success
    return (REP if _repaired(loop, case.tool, args) else OK), text


async def run_case(case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    proj = _project(tmp_path)
    loop = _loop(proj, monkeypatch, mode=case.extra.get("mode", "yolo"))
    if case.setup:
        case.setup(proj, loop)
    args = _subst(case.args, proj)
    result = await asyncio.wait_for(loop._execute_tool(ToolCall(id="c", name=case.tool, arguments=args)), 20)
    return classify(case, proj, loop, args, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_case(case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, text = await run_case(case, tmp_path, monkeypatch)
    assert case.known_gap == (case.expect == OPQ), f"{case.id}: known_gap must mark exactly the opaque cases"
    assert outcome == case.expect, f"{case.id}: got {outcome}, model saw: {text[:300]!r}"


def test_corpus_size() -> None:
    assert len(CASES) > 100
    assert len({c.id for c in CASES}) == len(CASES)


def test_summary() -> None:
    """The counts per outcome class, and family x tool, from the table (``-k summary -s`` prints them)."""
    by_class = Counter(c.expect for c in CASES)
    print(f"\ncases: {len(CASES)}  " + "  ".join(f"{k}={by_class.get(k, 0)}" for k in (OK, REP, ACT, OPQ)))
    grid: dict[tuple[str, str], Counter[str]] = {}
    for c in CASES:
        grid.setdefault((c.family, c.tool), Counter())[c.expect] += 1
    for (family, tool), counts in sorted(grid.items()):
        print(f"{family:20} {tool:12} " + " ".join(f"{k}={counts[k]}" for k in (OK, REP, ACT, OPQ) if counts[k]))
    assert sum(by_class.values()) == len(CASES)
