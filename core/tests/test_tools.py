"""Tool registry and implementation tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from k3code.tools import build_registry, tool_bash, tool_edit, tool_glob, tool_grep, tool_read, tool_write

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ── Registry tests ─────────────────────────────────────────────────────


def test_registry_has_all_tools():
    reg = build_registry()
    names = reg.names()
    assert set(names) == {
        "read",
        "write",
        "edit",
        "bash",
        "bash_output",
        "bash_kill",
        "grep",
        "glob",
        "todo",
        "exit_plan",
    }
    for name in names:
        spec, handler = reg.get(name)
        assert spec.name == name
        assert spec.parameters is not None
        assert callable(handler)


def test_side_effect_flags():
    reg = build_registry()
    pure = {"read", "grep", "glob", "todo", "exit_plan"}
    side_effect = {"write", "edit", "bash"}
    for name in pure:
        spec, _ = reg.get(name)
        assert spec.side_effect is False, f"{name} should be pure"
    for name in side_effect:
        spec, _ = reg.get(name)
        assert spec.side_effect is True, f"{name} should have side_effect"


# ── Tool tests ─────────────────────────────────────────────────────────


async def test_read_file(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("line1\nline2\nline3\n")
    result = await tool_read({"path": str(f)}, cwd=temp_dir)
    assert "line1" in result["content"]
    assert result["lines"] == "1-3 of 3"


async def test_read_line_range(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("line1\nline2\nline3\nline4\nline5\n")
    result = await tool_read({"path": str(f), "start": 2, "end": 4}, cwd=temp_dir)
    assert result["content"] == "line2\nline3\nline4"
    assert result["lines"] == "2-4 of 5"


async def test_read_of_a_huge_file_is_refused_or_streamed_as_a_range(temp_dir, monkeypatch):
    import k3code.tools as tools

    monkeypatch.setattr(tools, "MAX_READ_BYTES", 100)
    f = temp_dir / "big.log"
    f.write_text("".join(f"line{i}\n" for i in range(1, 51)))  # ~340 bytes
    result = await tool_read({"path": str(f)}, cwd=temp_dir)
    assert "start and end" in result["error"]
    result = await tool_read({"path": str(f), "start": 10, "end": 12}, cwd=temp_dir)
    assert result["content"] == "line10\nline11\nline12"
    assert result["lines"] == "10-12"
    assert "error" in await tool_read({"path": str(f), "start": 1, "end": 50}, cwd=temp_dir)
    f.write_text("x" * 500 + "\nafter\n")  # one line longer than the limit is skipped, not loaded
    result = await tool_read({"path": str(f), "start": 2, "end": 2}, cwd=temp_dir)
    assert result["content"] == "after"
    assert "error" in await tool_read({"path": str(f), "start": 1, "end": 1}, cwd=temp_dir)


async def test_a_large_binary_file_is_not_streamed_as_text_either(temp_dir, monkeypatch):
    import k3code.tools as tools

    monkeypatch.setattr(tools, "MAX_READ_BYTES", 100)
    f = temp_dir / "disk.img"
    f.write_bytes(b"\x7fELF\x00\x01" + b"\xff\x00" * 200)
    result = await tool_read({"path": str(f), "start": 1, "end": 5}, cwd=temp_dir)
    assert "binary file" in result["error"]


async def test_a_large_file_that_starts_with_nul_padding_is_still_read_by_range(temp_dir, monkeypatch):
    import k3code.tools as tools

    monkeypatch.setattr(tools, "MAX_READ_BYTES", 100)
    f = temp_dir / "app.log"  # a log that was truncated in place keeps NULs at its start
    f.write_bytes(b"\x00" * 200 + b"\n" + "".join(f"line{i}\n" for i in range(2, 30)).encode())
    result = await tool_read({"path": str(f), "start": 2, "end": 4}, cwd=temp_dir)
    assert result["content"] == "line2\nline3\nline4"
    result = await tool_read({"path": str(f), "offset": 10, "limit": 2}, cwd=temp_dir)
    assert result["content"] == "line10\nline11"
    # a window that does hold the NULs is not text
    assert "binary file" in (await tool_read({"path": str(f), "start": 1, "end": 3}, cwd=temp_dir))["error"]


async def test_write_file(temp_dir):
    f = temp_dir / "new.txt"
    result = await tool_write({"path": str(f), "content": "hello"}, cwd=temp_dir)
    assert result["ok"] is True
    assert f.read_text() == "hello"


async def test_write_creates_parents(temp_dir):
    f = temp_dir / "sub" / "dir" / "file.txt"
    result = await tool_write({"path": str(f), "content": "nested"}, cwd=temp_dir)
    assert result["ok"] is True
    assert f.read_text() == "nested"


async def test_edit_exact_match(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("foo\nbar\nbaz\n")
    result = await tool_edit({"path": str(f), "old_string": "bar", "new_string": "BAR"}, cwd=temp_dir)
    assert result["ok"] is True
    assert result["replacements"] == 1
    assert f.read_text() == "foo\nBAR\nbaz\n"


async def test_edit_not_found(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("foo\nbar\n")
    result = await tool_edit({"path": str(f), "old_string": "missing", "new_string": "x"}, cwd=temp_dir)
    assert "error" in result
    assert "Could not find" in result["error"]


async def test_edit_ambiguous(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("foo\nfoo\n")
    result = await tool_edit({"path": str(f), "old_string": "foo", "new_string": "bar"}, cwd=temp_dir)
    assert "error" in result
    assert "Found 2 matches" in result["error"]


async def test_edit_replace_all(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("foo\nfoo\n")
    result = await tool_edit(
        {"path": str(f), "old_string": "foo", "new_string": "bar", "replace_all": True}, cwd=temp_dir
    )
    assert result["ok"] is True
    assert result["replacements"] == 2
    assert f.read_text() == "bar\nbar\n"


async def test_bash_simple(temp_dir):
    result = await tool_bash({"command": "echo hello"}, cwd=temp_dir)
    assert result["stdout"].strip() == "hello"
    assert result["exit_code"] == 0


async def test_bash_timeout_kill(temp_dir):
    # Sleep longer than timeout
    result = await tool_bash({"command": "sleep 10", "timeout": 0.1}, cwd=temp_dir)
    assert "error" in result
    assert "timed out" in result["error"].lower()
    assert result["exit_code"] == -1


async def test_grep_finds(temp_dir):
    (temp_dir / "a.py").write_text("def foo():\n    pass\n")
    (temp_dir / "b.py").write_text("def bar():\n    pass\n")
    result = await tool_grep({"pattern": "def foo", "path": "."}, cwd=temp_dir)
    assert "a.py" in result["matches"]
    assert "foo" in result["matches"]


def test_bash_does_not_read_the_gateways_stdin(temp_dir):
    # In stdio mode the gateway's stdin is the TUI's JSON-RPC stream: a command must never consume it.
    import subprocess
    import sys

    script = (
        "import asyncio, pathlib, sys\n"
        "from k3code.tools import tool_bash\n"
        "r = asyncio.run(tool_bash({'command': 'head -c 40'}, cwd=pathlib.Path(sys.argv[1])))\n"
        "print(repr(r['stdout']))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script, str(temp_dir)],
        input='{"jsonrpc":"2.0","method":"x"}\n',
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert out.stdout.strip() == "''"


async def test_grep_pattern_starting_with_a_dash(temp_dir):
    (temp_dir / "a.txt").write_text("x\n--force here\n")
    result = await tool_grep({"pattern": "--force", "path": "."}, cwd=temp_dir)
    assert "--force here" in result["matches"]
    assert "error" not in result


async def test_grep_no_matches(temp_dir):
    (temp_dir / "a.py").write_text("x = 1\n")
    result = await tool_grep({"pattern": "def foo", "path": "."}, cwd=temp_dir)
    assert result["matches"] == ""


async def test_grep_pattern_that_looks_like_a_flag_is_searched_as_text(temp_dir):
    # Without -e/--, rg reads "--files" as its own option and lists file names instead of matching the line.
    (temp_dir / "notes.txt").write_text("the flag --files is literal here\n")
    result = await tool_grep({"pattern": "--files", "path": "."}, cwd=temp_dir)
    assert "notes.txt\n1:the flag --files is literal here" in result["matches"]


# ── grep never hands a credential file to the model (K3NOX-68, GitHub #138) ──


def _secret_project(root: Path) -> Path:
    (root / ".env").write_text("API_KEY=sk-dotenv-secret\n")
    (root / ".env.local").write_text("API_KEY=sk-local-secret\n")
    (root / ".env.example").write_text("API_KEY=example-template\n")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "a.js").write_text("API_KEY in a dependency\n")
    (root / "app.py").write_text("API_KEY = os.environ['API_KEY']\n")
    return root


def _flat(matches: str) -> str:
    """grep's grouped result (a file's path, then its ``line:text`` matches) as ``path:line:text`` lines."""
    out, current = [], ""
    for line in matches.splitlines():
        number, sep, _ = line.partition(":")
        if sep and number.isdigit() and current:
            out.append(f"{current}:{line}")
        elif line:
            current = line
    return "\n".join(out)


def _no_rg(monkeypatch, root: Path) -> None:
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("PATH", str(root / "no-bin"))  # no rg (and no git): the Python fallback


@pytest.fixture
def rg():
    import shutil

    if not shutil.which("rg"):
        pytest.skip("rg not installed")


@pytest.mark.parametrize("include", ["*", ".env*", ["*.py", ".*"], "**"])
async def test_grep_include_glob_does_not_reopen_dotenv_files_or_node_modules(temp_dir, rg, include):
    # rg's last matching glob wins and an include glob overrides its hidden-file filter: `include: "*"` used to
    # return .env (and node_modules) while read/grep of `.env` itself is a sensitive-path hardline deny.
    _secret_project(temp_dir)
    res = await tool_grep({"pattern": "API_KEY", "path": ".", "include": include}, cwd=temp_dir)
    assert "sk-dotenv-secret" not in res["matches"] and "sk-local-secret" not in res["matches"]
    assert "node_modules" not in res["matches"]
    if include != ".env*":
        assert "app.py:1:API_KEY" in _flat(res["matches"])
    # rg's credential excludes (grep_excludes) keep every .env.* out, templates too: stricter than sensitive_path,
    # which the Python fallback applies (test_grep_fallback_skips_dotenv_files still finds the template there)


async def test_grep_ignore_file_negation_does_not_reopen_dotenv(temp_dir, rg):
    # A hostile repository can whitelist its .env for rg without any include: `!.env` in .ignore or .gitignore.
    _secret_project(temp_dir)
    (temp_dir / ".ignore").write_text("!.env\n!.env.local\n")
    res = await tool_grep({"pattern": "API_KEY", "path": "."}, cwd=temp_dir)
    assert "sk-dotenv-secret" not in res["matches"] and "sk-local-secret" not in res["matches"]
    assert "app.py:1:API_KEY" in _flat(res["matches"])


async def test_grep_include_can_still_search_inside_node_modules_by_path(temp_dir, rg):
    _secret_project(temp_dir)
    res = await tool_grep({"pattern": "API_KEY", "path": "node_modules/pkg", "include": "*.js"}, cwd=temp_dir)
    assert "a.js:1:API_KEY in a dependency" in _flat(res["matches"])


async def test_grep_fallback_skips_dotenv_files(temp_dir, monkeypatch):
    # The Python fallback walks hidden files (and `git ls-files -co` lists a .env that is not ignored).
    _secret_project(temp_dir)
    _no_rg(monkeypatch, temp_dir)
    for include in (None, "*", ".env*"):
        args = {"pattern": "API_KEY", "path": ".", **({"include": include} if include else {})}
        res = await tool_grep(args, cwd=temp_dir)
        assert "sk-dotenv-secret" not in res["matches"] and "sk-local-secret" not in res["matches"], include
        assert "API_KEY=example-template" in res["matches"], include
    assert "app.py" in (await tool_grep({"pattern": "API_KEY", "path": "."}, cwd=temp_dir))["matches"]


@pytest.mark.parametrize("fallback", [False, True])
async def test_grep_of_a_home_directory_skips_key_and_k3code_secret_files(temp_dir, monkeypatch, fallback):
    import shutil

    if not fallback and not shutil.which("rg"):
        pytest.skip("rg not installed")
    home = temp_dir / "home"
    for rel, text in {
        ".ssh/id_ed25519": "API_KEY ssh-private-key",
        ".aws/credentials": "API_KEY aws_secret_access_key",
        ".config/k3code/env": "API_KEY=sk-daemon-env",
        "notes/todo.txt": "API_KEY rotate me",
    }.items():
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(text + "\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    if fallback:
        _no_rg(monkeypatch, temp_dir)
    res = await tool_grep({"pattern": "API_KEY", "path": str(home), "include": "*"}, cwd=temp_dir)
    assert "todo.txt:1:API_KEY rotate me" in _flat(res["matches"])
    for secret in ("ssh-private-key", "aws_secret_access_key", "sk-daemon-env"):
        assert secret not in res["matches"]


@pytest.mark.parametrize("fallback", [False, True])
async def test_grep_of_a_dotenv_file_itself_is_refused(temp_dir, monkeypatch, fallback):
    _secret_project(temp_dir)
    if fallback:
        _no_rg(monkeypatch, temp_dir)
    res = await tool_grep({"pattern": "API_KEY", "path": ".env"}, cwd=temp_dir)
    assert "sk-dotenv-secret" not in str(res)
    assert "sensitive-path" in res["error"]
    template = await tool_grep({"pattern": "API_KEY", "path": ".env.example"}, cwd=temp_dir)
    assert "example-template" in template["matches"]


async def test_grep_falls_back_to_the_filtered_python_search_when_rg_cannot_start(temp_dir, monkeypatch):
    # rg on PATH but not runnable (no exec permission, a broken binary): any OSError at spawn, not only "not found"
    _secret_project(temp_dir)
    real = asyncio.create_subprocess_exec

    async def unrunnable_rg(*cmd, **kwargs):
        if cmd and cmd[0] == "rg":
            raise PermissionError(13, "Permission denied", "rg")
        return await real(*cmd, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unrunnable_rg)
    res = await tool_grep({"pattern": "API_KEY", "path": "."}, cwd=temp_dir)
    assert "app.py:1:API_KEY" in _flat(res["matches"])
    assert "sk-dotenv-secret" not in res["matches"] and "sk-local-secret" not in res["matches"]


async def test_a_cancelled_grep_does_not_leave_rg_running(temp_dir, monkeypatch):
    import k3code.tools as tools

    started: list[asyncio.subprocess.Process] = []
    real = asyncio.create_subprocess_exec

    async def spawn(*cmd, **kwargs):
        started.append(await real(*cmd, **kwargs))
        return started[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(tools._run_rg(["sleep", "30"], temp_dir, temp_dir, named=True))  # an rg that hangs
    while not started:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.wait_for(started[0].wait(), timeout=5) is not None


_HOME_SECRETS = {
    ".ssh/id_ed25519": "API_KEY ssh-private-key",
    ".aws/credentials": "API_KEY aws_secret_access_key",
    ".config/k3code/env": "API_KEY=sk-daemon-env",
    "notes/todo.txt": "API_KEY rotate me",
}


def _fake_home(root: Path, monkeypatch) -> Path:
    home = root / "home"
    for rel, text in _HOME_SECRETS.items():
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(text + "\n")
    (home / "proj").mkdir()
    (home / "proj" / "app.py").write_text("API_KEY = 1\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.mark.parametrize("include", [None, "*"])
async def test_grep_of_home_from_a_project_in_it_never_searches_the_key_directories(temp_dir, rg, monkeypatch, include):
    # K3NOX-106: the session is ~/proj and the search is ~ (an added directory). rg anchors its globs where it runs,
    # so the credential excludes have to be built for (and rg run at) the search root, not the session's directory.
    import k3code.tools as tools

    home = _fake_home(temp_dir, monkeypatch)
    reached: list[str] = []
    real = tools.sensitive_path

    def spy(path: str) -> bool:
        reached.append(path)
        return real(path)

    monkeypatch.setattr(tools, "sensitive_path", spy)
    args = {"pattern": "API_KEY", "path": str(home), **({"include": include} if include else {})}
    res = await tool_grep(args, cwd=home / "proj")
    flat = _flat(res["matches"])
    assert f"{home}/notes/todo.txt:1:API_KEY rotate me" in flat and "app.py:1:API_KEY = 1" in flat
    for secret in ("ssh-private-key", "aws_secret_access_key", "sk-daemon-env"):
        assert secret not in res["matches"]
    # rg pruned the key directories: no file in them came back for the result filter to drop
    assert not [p for p in reached if any(d in p for d in ("/.ssh", "/.aws", "/.config/k3code"))], reached


async def test_grep_checks_a_file_name_that_holds_a_newline_as_one_name(temp_dir, rg, monkeypatch):
    # rg prints a newline in a file name as it is, so the name came in two lines: the part before the newline passed
    # unchecked, the part after it named no real file. Here `proj/a<newline>b` is a symlink to the home directory.
    home = _fake_home(temp_dir, monkeypatch)
    proj = home / "proj"
    (proj / "a\nb").symlink_to(home)
    (proj / "x\ny.py").write_text("API_KEY = 2\n")
    res = await tool_grep({"pattern": "API_KEY", "path": str(proj / "a\nb")}, cwd=proj)
    for secret in ("ssh-private-key", "aws_secret_access_key", "sk-daemon-env"):
        assert secret not in res["matches"]
    assert "API_KEY rotate me" in res["matches"]
    found = (await tool_grep({"pattern": "API_KEY", "path": "."}, cwd=proj))["matches"]
    assert "x\ny.py\n1:API_KEY = 2" in found and "app.py\n1:API_KEY = 1" in found  # a newline name still shows


async def test_grep_through_a_symlink_to_home_never_searches_the_key_directories(temp_dir, rg, monkeypatch):
    # the search root is inside the project by its path but is the home directory by its realpath: the credential
    # excludes are built where rg really searches
    import k3code.tools as tools

    home = _fake_home(temp_dir, monkeypatch)
    (home / "proj" / "link").symlink_to(home)
    reached: list[str] = []
    real = tools.sensitive_path
    monkeypatch.setattr(tools, "sensitive_path", lambda p: reached.append(p) or real(p))
    link = str(home / "proj" / "link")  # absolute: a relative path is resolved before the search, this one is not
    res = await tool_grep({"pattern": "API_KEY", "path": link, "include": "*"}, cwd=home / "proj")
    assert "API_KEY rotate me" in res["matches"]
    assert not [p for p in reached if any(d in p for d in ("/.ssh", "/.aws", "/.config/k3code"))], reached


async def test_grep_skips_dependency_folders_when_the_project_itself_sits_under_a_skipped_name(temp_dir, rg):
    # only the part of the path below the session's directory says whether the search is inside node_modules on purpose
    proj = temp_dir / ".cache" / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "node_modules" / "m").mkdir(parents=True)
    (proj / "src" / "a.js").write_text("NEEDLE src\n")
    (proj / "node_modules" / "m" / "i.js").write_text("NEEDLE dep\n")
    res = await tool_grep({"pattern": "NEEDLE"}, cwd=proj)
    assert "src/a.js" in res["matches"] and "node_modules" not in res["matches"]


async def test_grep_drops_a_sensitive_file_even_when_rg_reaches_it(temp_dir, rg, monkeypatch):
    # the result filter on its own: with the credential excludes gone, rg searches ~/.ssh and the rest, and still
    # nothing from them reaches the model
    import k3code.tools as tools

    home = _fake_home(temp_dir, monkeypatch)
    monkeypatch.setattr(tools, "grep_excludes", lambda base: [])
    res = await tool_grep({"pattern": "API_KEY", "path": str(home), "include": "*"}, cwd=home / "proj")
    assert f"{home}/notes/todo.txt:1:API_KEY rotate me" in _flat(res["matches"])
    for secret in ("ssh-private-key", "aws_secret_access_key", "sk-daemon-env"):
        assert secret not in res["matches"]


def test_grep_excludes_prune_the_key_directories_under_real_rg(temp_dir, rg, monkeypatch):
    # the anchored `!/.ssh` globs grep_excludes builds, checked against the rg on this host (not only as strings)
    import subprocess

    from k3code.permissions.engine import grep_excludes

    home = _fake_home(temp_dir, monkeypatch)
    (home / "proj" / ".env").write_text("API_KEY=sk-dotenv\n")
    cmd = ["rg", "--files", "--hidden", "-g", "*"]  # an include glob: the excludes must win over it
    for glob in grep_excludes(str(home)):
        cmd += ["--iglob", glob]
    listed = subprocess.run(cmd, cwd=home, capture_output=True, text=True, check=False).stdout.splitlines()
    assert sorted(listed) == ["notes/todo.txt", "proj/app.py"], listed


async def test_glob_finds(temp_dir):
    (temp_dir / "a.py").write_text("")
    (temp_dir / "b.txt").write_text("")
    (temp_dir / "sub").mkdir(exist_ok=True)
    (temp_dir / "sub" / "c.py").write_text("")
    result = await tool_glob({"pattern": "*.py", "path": "."}, cwd=temp_dir)
    files = set(result["files"])
    assert "a.py" in files
    assert "sub/c.py" in files
    assert "b.txt" not in files


# ── Fuzzy match tests (via edit tool) ─────────────────────────────────


async def test_edit_whitespace_flexible(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("def foo():\n    return 1\n")
    # Extra spaces in old_string
    result = await tool_edit(
        {"path": str(f), "old_string": "def foo():  \n    return 1", "new_string": "def foo():\n    return 2"},
        cwd=temp_dir,
    )
    assert result["ok"] is True
    assert f.read_text() == "def foo():\n    return 2\n"


async def test_edit_indentation_flexible(temp_dir):
    f = temp_dir / "test.txt"
    f.write_text("  def foo():\n    return 1\n")
    # Less indentation in old_string
    result = await tool_edit(
        {"path": str(f), "old_string": "def foo():\n  return 1", "new_string": "def foo():\n  return 2"},
        cwd=temp_dir,
    )
    assert result["ok"] is True
    assert f.read_text() == "  def foo():\n    return 2\n"


# ── bash: bounded output, group kill on cancel/timeout (found by the long-run audit) ──


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie of a reaped-late child counts as gone
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


async def test_bash_keeps_the_whole_output_when_it_fits_the_capture(temp_dir):
    """The model's cut (clip_tool_results) happens later; the tool itself keeps everything it captured."""
    result = await tool_bash({"command": "head -c 12000 /dev/zero | tr '\\0' a"}, cwd=temp_dir)
    assert result["stdout"] == "a" * 12_000
    assert result["exit_code"] == 0


async def test_bash_capture_keeps_the_head_and_the_tail_and_counts_what_it_dropped(temp_dir):
    import k3code.tools as tools

    result = await tool_bash({"command": "seq 1 30000"}, cwd=temp_dir)
    full = "".join(f"{i}\n" for i in range(1, 30_001))
    dropped = len(full) - tools._KEEP_HEAD_BYTES - tools._KEEP_TAIL_BYTES
    expected = (
        full[: tools._KEEP_HEAD_BYTES]
        + f"\n... [truncated {dropped} bytes not kept by the capture] ...\n"
        + full[-tools._KEEP_TAIL_BYTES :]
    )
    assert result["stdout"] == expected
    assert result["stdout"].endswith("30000\n")


async def test_bash_flood_is_killed_instead_of_filling_daemon_memory(temp_dir, monkeypatch):
    """communicate() buffered every byte: `yes` took the daemon to 1 GB/s of RSS until the OOM killer ran."""
    import resource

    import k3code.tools as tools

    monkeypatch.setattr(tools, "MAX_OUTPUT_BYTES", 2 * 1024 * 1024)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = await asyncio.wait_for(tool_bash({"command": "yes", "timeout": 30}, cwd=temp_dir), 20)
    assert "Output limit exceeded" in result["error"] and result["exit_code"] == -9
    assert len(result["stdout"]) < tools._KEEP_HEAD_BYTES + tools._KEEP_TAIL_BYTES + 200  # head + tail + marker
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 100_000  # KB: far below the old GBs


async def test_bash_cancel_kills_the_whole_process_group(temp_dir):
    """/stop cancels the task; only the shell used to be waited on and `sleep 300` kept running."""
    pidfile = temp_dir / "child.pid"
    task = asyncio.create_task(
        tool_bash({"command": f"sleep 300 & echo $! > {pidfile}; wait", "timeout": 60}, cwd=temp_dir)
    )
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    child = int(pidfile.read_text())
    assert _alive(child)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert not _alive(child)


async def test_bash_timeout_returns_promptly_even_when_a_grandchild_holds_the_pipe(temp_dir):
    """wait() only finishes when the pipes close, so a background child used to hang the timeout path."""
    pidfile = temp_dir / "bg.pid"
    started = asyncio.get_running_loop().time()
    result = await asyncio.wait_for(
        tool_bash({"command": f"sleep 300 & echo $! > {pidfile}; sleep 300", "timeout": 0.5}, cwd=temp_dir), 15
    )
    assert "timed out" in result["error"] and result["exit_code"] == -1
    assert asyncio.get_running_loop().time() - started < 10
    await asyncio.sleep(0.3)
    assert not _alive(int(pidfile.read_text()))


# ── file tools: bytes outside an edit are preserved; writes are atomic ──


async def test_edit_keeps_crlf_line_endings(temp_dir):
    f = temp_dir / "win.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = await tool_edit({"path": str(f), "old_string": "two\nthree", "new_string": "2\nthree"}, cwd=temp_dir)
    assert result["ok"] is True
    assert f.read_bytes() == b"one\r\n2\r\nthree\r\n"  # one edit must not rewrite every line ending


async def test_edit_leaves_mixed_line_endings_alone(temp_dir):
    f = temp_dir / "mixed.txt"
    f.write_bytes(b"a\r\nb\nc\r\n")
    result = await tool_edit({"path": str(f), "old_string": "b", "new_string": "B"}, cwd=temp_dir)
    assert result["ok"] is True and f.read_bytes() == b"a\r\nB\nc\r\n"


async def test_edit_refuses_a_file_that_is_not_utf8_instead_of_destroying_its_bytes(temp_dir):
    f = temp_dir / "latin1.txt"
    f.write_bytes("caf\xe9 au lait\n".encode("latin-1"))
    result = await tool_edit({"path": str(f), "old_string": "au", "new_string": "AU"}, cwd=temp_dir)
    assert "not valid UTF-8" in result["error"]
    assert f.read_bytes() == "caf\xe9 au lait\n".encode("latin-1")  # untouched, 0xE9 intact


async def test_write_is_atomic_and_keeps_the_mode(temp_dir):
    f = temp_dir / "run.sh"
    f.write_text("old\n")
    f.chmod(0o755)
    assert (await tool_write({"path": str(f), "content": "new\n"}, cwd=temp_dir))["ok"] is True
    assert f.read_text() == "new\n" and (f.stat().st_mode & 0o777) == 0o755
    assert [p.name for p in temp_dir.iterdir() if "k3tmp" in p.name] == []  # no temp file left behind


async def test_write_through_a_symlink_keeps_the_link(temp_dir):
    real = temp_dir / "real.txt"
    real.write_text("x")
    link = temp_dir / "link.txt"
    link.symlink_to(real)
    await tool_write({"path": str(link), "content": "y"}, cwd=temp_dir)
    assert link.is_symlink() and real.read_text() == "y"


async def test_read_numbers_lines_like_an_editor(temp_dir):
    f = temp_dir / "ff.txt"
    f.write_text("a\x0cb\nc\nd\n")  # a form feed is not a line break for editors (str.splitlines() splits on it)
    result = await tool_read({"path": str(f), "start": 2, "end": 2}, cwd=temp_dir)
    assert result["content"] == "c" and result["lines"] == "2-2 of 3"
