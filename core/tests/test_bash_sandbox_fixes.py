"""The bash tool, background jobs and the sandbox: polling a quiet job, two-stream clipping, the hints a sandboxed
failure gets, the child environment, CA bundles, a missing cwd, a refused write, a read past the end, timeouts."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
import types
from pathlib import Path

import pytest

from k3code.agent.loop import AgentLoop
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.reliability import sandbox
from k3code.reliability.loopguard import LoopGuard, Verdict
from k3code.router import Router, build_chain
from k3code.tools import (
    _sandbox_hint,
    format_tool_result,
    jobs,
    tool_bash,
    tool_bash_output,
    tool_edit,
    tool_read,
    tool_write,
)


@pytest.fixture
async def clean_jobs():
    yield
    await jobs.REGISTRY.reap_all()


def _job_id(result: dict) -> str:
    m = re.search(r"started (job\d+) \(pid \d+\)", result["content"])
    assert m, result
    return m.group(1)


# ── bash_output: polling a quiet job ──────────────────────────────────


class _Script:
    """Plays one tool call per model call, then a final text."""

    name = "fake"
    base_url = "https://fake.test"

    def __init__(self, calls: list[ToolCall]) -> None:
        self.calls, self.n = calls, 0

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        tc = self.calls[self.n] if self.n < len(self.calls) else None
        self.n += 1
        msg = Message(role="assistant", content=None if tc else "done", tool_calls=[tc] if tc else [])
        yield StreamEvent(type="done", message=msg, usage=Usage())

    async def aclose(self) -> None:
        pass


def _poll_loop(tmp: Path, polls: int, job: str, **kw) -> AgentLoop:
    calls = [ToolCall(id=f"c{i}", name="bash_output", arguments={"job_id": job}) for i in range(polls)]
    return AgentLoop(
        Router(build_chain([_Script(calls)], [["m"]]), max_retries=0),
        system_prompt="t",
        permission_mode="yolo",
        cwd=tmp,
        max_turns=30,
        session="poll",
        **kw,
    )


async def test_polling_a_quiet_running_job_is_not_a_loop(tmp_path, monkeypatch, clean_jobs):
    """A job that prints nothing for a while answers every poll with the same words; polls a model call apart are
    time passing, not repetition, so the loop guard must not stop the turn on the fourth."""
    real = time.time
    ticks = iter(range(1, 1000))
    # five seconds pass between two reads: what a model round trip takes, which a scripted provider does not
    monkeypatch.setattr(jobs, "time", types.SimpleNamespace(time=lambda: real() + 5 * next(ticks)))
    job = _job_id(await tool_bash({"command": "sleep 30", "background": True}, cwd=tmp_path, session_id="poll"))
    loop = _poll_loop(tmp_path, 8, job)
    events = [e async for e in loop.run("wait for the build")]
    tool_texts = [m.content or "" for m in loop.turn_messages if m.role == "tool"]
    assert len(tool_texts) == 8 and not any("loop guard" in t for t in tool_texts), tool_texts
    assert all(t.startswith(f"[{job} running (pid ") for t in tool_texts)
    assert len(set(tool_texts)) == 8  # every poll reads differently: the job is older
    assert events[-1].message.content == "done" and loop.escalation_reason is None


def test_the_same_words_over_and_over_still_trip_the_guard_for_an_exited_job():
    """An exited job (or a bad id) answers the same every time: that is a real loop and is still stopped."""
    guard = LoopGuard()
    verdicts = []
    for _ in range(4):
        outcome = guard.observe_tool_call("bash_output", {"job_id": "job1"})
        guard.observe_tool_result("bash_output", {"job_id": "job1"}, None, "bash_output job1", "same")
        verdicts.append(outcome.verdict)
    assert verdicts[2] is Verdict.NOTE and verdicts[3] is Verdict.STOP


async def test_polling_an_unknown_job_is_still_stopped(tmp_path, clean_jobs):
    loop = _poll_loop(tmp_path, 8, "job-none")
    events = [e async for e in loop.run("poll")]
    tool_texts = [m.content or "" for m in loop.turn_messages if m.role == "tool"]
    assert tool_texts[0].startswith("Error: no job 'job-none' in this session")
    assert any("loop guard" in t for t in tool_texts) and loop.escalation_reason == "loop_guard"
    assert "repeating myself" in (events[-1].message.content or "")


async def test_a_running_job_reads_with_its_age(tmp_path, clean_jobs):
    job = _job_id(await tool_bash({"command": "sleep 30", "background": True}, cwd=tmp_path, session_id="age"))
    out = (await tool_bash_output({"job_id": job}, session_id="age"))["content"]
    assert re.match(rf"\[{job} running \(pid \d+\) for \d+s\]\n\(no new output; pass wait", out), out


async def test_wait_blocks_until_the_job_prints(tmp_path, clean_jobs):
    job = _job_id(
        await tool_bash({"command": "sleep 0.4; echo built", "background": True}, cwd=tmp_path, session_id="w1")
    )
    start = time.monotonic()
    out = (await tool_bash_output({"job_id": job, "wait": 10}, session_id="w1"))["content"]
    assert "built" in out and 0.3 < time.monotonic() - start < 5, out


async def test_wait_returns_when_the_job_exits_silently(tmp_path, clean_jobs):
    job = _job_id(await tool_bash({"command": "sleep 0.3", "background": True}, cwd=tmp_path, session_id="w2"))
    start = time.monotonic()
    out = (await tool_bash_output({"job_id": job, "wait": 10}, session_id="w2"))["content"]
    assert out == f"[{job} exited with code 0]\n(no new output)" and time.monotonic() - start < 5


async def test_wait_gives_up_on_a_quiet_job_and_says_it_still_runs(tmp_path, clean_jobs):
    job = _job_id(await tool_bash({"command": "sleep 30", "background": True}, cwd=tmp_path, session_id="w3"))
    start = time.monotonic()
    out = (await tool_bash_output({"job_id": job, "wait": 0.3}, session_id="w3"))["content"]
    assert f"[{job} running" in out and 0.25 < time.monotonic() - start < 5


async def test_wait_does_not_block_when_output_is_already_there(tmp_path, clean_jobs):
    job = _job_id(
        await tool_bash({"command": "echo first; sleep 30", "background": True}, cwd=tmp_path, session_id="w4")
    )
    await asyncio.sleep(0.3)
    start = time.monotonic()
    out = (await tool_bash_output({"job_id": job, "wait": 20}, session_id="w4"))["content"]
    assert "first" in out and time.monotonic() - start < 3


async def test_wait_is_capped_and_must_be_a_number(tmp_path, clean_jobs):
    job = _job_id(await tool_bash({"command": "sleep 0.2", "background": True}, cwd=tmp_path, session_id="w5"))
    out = (await tool_bash_output({"job_id": job, "wait": 30000}, session_id="w5"))[
        "content"
    ]  # milliseconds, as CC sends
    assert out.endswith(f"[wait 30000 was cut to {jobs.MAX_WAIT_S}: it is in seconds]")
    bad = await tool_bash_output({"job_id": job, "wait": "soon"}, session_id="w5")
    assert bad["error"].startswith("wait must be a number of seconds")


# ── a command that writes to both streams ─────────────────────────────


def _bash_result(stdout: str, stderr: str, code: int) -> dict:
    return {"stdout": stdout, "stderr": stderr, "exit_code": code}


def test_the_end_of_stdout_survives_when_stderr_is_long_too():
    """cargo test: 25k chars of compiler noise on stderr, 15k of test output on stdout. Cut as one text the summary
    and the panic message (the end of stdout) fell in the dropped middle."""
    from k3code.tools import clip_for_model, format_tool_result

    stderr = "".join(f"   Compiling crate{i} v0.{i}.0 (/work/vendor/crate{i})\n" for i in range(400))
    tests = "".join(f"test tests::case_{i} ... ok\n" for i in range(600))
    ends = (
        "test tests::case_failing ... FAILED\npanicked at src/lib.rs:42:5\ntest result: FAILED. 600 passed; 1 failed\n"
    )
    shown = clip_for_model("bash", format_tool_result(_bash_result(tests + ends, stderr, 101)), 10_000)
    for needle in (
        "test result: FAILED",
        "panicked at",
        "case_failing",
        "[exit code 101]",
        "Compiling crate399",
        "[stderr]",
    ):
        assert needle in shown, needle
    assert "test tests::case_0 ... ok" in shown and "Compiling crate0 " in shown  # and each stream's start
    assert len(shown) < 10_000 + 600  # the budget, plus the two markers that say what was cut


def test_a_stream_that_needs_little_leaves_the_rest_to_the_other():
    from k3code.tools import clip_for_model, format_tool_result

    stderr = "".join(f"warning {i}: unused variable\n" for i in range(1500))  # ~42k chars
    shown = clip_for_model("bash", format_tool_result(_bash_result("2 passed\n", stderr, 1)), 10_000)
    assert shown.startswith("2 passed\n[stderr]\nwarning 0:") and shown.endswith("[exit code 1]")
    assert len(shown) < 10_000 + 400 and "warning 1499:" in shown
    quiet = clip_for_model("bash", format_tool_result(_bash_result("x" * 30_000, "note\n", 0)), 10_000)
    assert quiet.endswith("[stderr]\nnote")  # the short stderr is whole


def test_one_stream_is_clipped_as_before():
    """A pytest-shaped run (all on stdout) and a command that only writes stderr are cut exactly as one text."""
    from k3code.tools import _CLIP_HINTS, clip_for_model, clip_head_tail, format_tool_result

    for result in (_bash_result("".join(f"line {i}\n" for i in range(5000)), "", 1), _bash_result("", "e" * 30_000, 2)):
        text = format_tool_result(result)
        assert clip_for_model("bash", text, 10_000) == clip_head_tail(text, 10_000, _CLIP_HINTS["bash"])


# ── what a sandboxed failure means ────────────────────────────────────

BWRAP_OK = bool(shutil.which("bwrap")) and sandbox.usable()
needs_bwrap = pytest.mark.skipif(not BWRAP_OK, reason="bwrap unavailable here")


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["config", "user.email", "a@b.c"], ["config", "user.name", "x"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=path, check=True, capture_output=True)
    return path


def test_a_tool_missing_in_the_sandbox_says_where_the_sandbox_looks():
    """node, cargo and uv under ~ or /opt are not visible: a bare "command not found" reads as "not installed"."""
    hint = _sandbox_hint("/bin/bash: line 1: node: command not found", "node -v", exit_code=127)
    assert "only /usr, /etc and the project are visible" in hint and "sandbox.home_readonly" in hint
    assert "sandbox.home_readonly" in _sandbox_hint("sh: 1: npm: not found", "npm -v", exit_code=127)  # dash
    assert "sandbox.home_readonly" in _sandbox_hint("make: node: not found", "make", exit_code=127)  # in a script
    # a typo is the same error to the shell; any other code or text is not this
    assert _sandbox_hint("bash: line 1: node: command not found", "node -v", exit_code=1) == ""
    assert _sandbox_hint("boom", "./run.sh", exit_code=127) == ""
    assert _sandbox_hint("bash: line 1: node: command not found", "node -v") == ""


@needs_bwrap
async def test_a_sandboxed_command_that_is_not_there_gets_the_hint(tmp_path):
    argv = sandbox.build_argv(tmp_path)
    res = await tool_bash({"command": "k3-no-such-tool --version"}, cwd=tmp_path, sandbox=argv)
    assert res["exit_code"] == 127 and "sandbox.home_readonly" in res["note"]
    plain = await tool_bash({"command": "k3-no-such-tool --version"}, cwd=tmp_path)  # unsandboxed: unchanged
    assert plain["exit_code"] == 127 and "note" not in plain


def test_a_silent_git_failure_is_the_read_only_git_dir():
    hint = _sandbox_hint("", "git stash", exit_code=1, silent=True)
    assert ".git is read-only" in hint
    for cmd in (
        "git stash push -u",
        "cd sub && git commit -am x",
        "git -C sub checkout main",
        "git add . ; git commit",
    ):
        assert ".git is read-only" in _sandbox_hint("", cmd, exit_code=1, silent=True), cmd
    # commands that fail with no output by design are not the sandbox
    for cmd in ("git diff --quiet", "git grep stash", "git config --get user.nope", "git check-ignore x", "ls"):
        assert _sandbox_hint("", cmd, exit_code=1, silent=True) == "", cmd
    assert _sandbox_hint("", "git stash", exit_code=1, silent=False) == ""  # it said something: the text speaks


@needs_bwrap
async def test_git_stash_in_the_sandbox_explains_itself(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("changed\n")
    res = await tool_bash({"command": "git stash"}, cwd=repo, sandbox=sandbox.build_argv(repo))
    assert res["exit_code"] == 1 and res["stdout"] == "" and res["stderr"] == ""
    assert ".git is read-only" in res["note"]


def test_a_missing_file_is_recognised_whatever_the_case_of_the_message():
    node = "Error: ENOENT: no such file or directory, open '/tmp/k3x/data.json'"
    go = "open /tmp/k3x/data.txt: no such file or directory"
    for text in (node, go):
        assert "private to each bash call" in _sandbox_hint(text, "run"), text
    assert _sandbox_hint("Error: ENOENT: no such file or directory, open 'data.json'", "run") == ""


def test_a_hidden_ca_bundle_is_explained():
    curl = "curl: (77) error setting certificate file: /tmp/x/ca.pem"
    pip = "OSError: Could not find a suitable TLS CA certificate bundle, invalid path: /home/u/ca.pem"
    git = "fatal: unable to access 'https://h/r.git/': error setting certificate verify locations:  CAfile: /tmp/ca"
    for text in (curl, pip, git):
        assert "CA bundle" in _sandbox_hint(text, "fetch", exit_code=77), text


# ── the environment of a child ────────────────────────────────────────


def test_a_child_cannot_wait_for_an_editor_pager_or_prompt():
    env = sandbox.child_env()
    assert env["GIT_EDITOR"] == "true" and env["GIT_SEQUENCE_EDITOR"] == "false"
    assert env["EDITOR"] == env["VISUAL"] == "false" and env["PAGER"] == env["GIT_PAGER"] == "cat"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert sandbox.child_env({"EDITOR": "nano"})["EDITOR"] == "nano"  # an MCP server's own env still wins


def test_the_sandbox_sets_the_same_environment(tmp_path):
    argv = sandbox.build_argv(tmp_path, bwrap="/usr/bin/bwrap")
    triples = [argv[i : i + 3] for i, a in enumerate(argv) if a == "--setenv"]
    for name, value in sandbox.NONINTERACTIVE_ENV.items():
        assert ["--setenv", name, value] in triples, name


async def test_git_does_not_wait_for_an_editor_in_a_repo_that_names_one(tmp_path):
    """Whatever core.editor says (vim on most machines), no one is there to type: commit fails at once."""
    repo = _repo(tmp_path / "repo")
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "two"], cwd=repo, check=True)
    for key in ("core.editor", "sequence.editor"):
        subprocess.run(["git", "config", key, "sleep 60"], cwd=repo, check=True)
    start = time.monotonic()
    commit = await tool_bash({"command": "git commit --allow-empty"}, cwd=repo)
    assert commit["exit_code"] == 1 and "Aborting commit" in commit["stderr"], commit
    rebase = await tool_bash({"command": "git rebase -i HEAD~1"}, cwd=repo)
    assert rebase["exit_code"] == 1 and "problem with the editor" in rebase["stderr"], rebase
    assert not (repo / ".git" / "rebase-merge").exists()
    assert time.monotonic() - start < 20  # not the 120 s timeout


async def test_a_command_that_sets_its_own_editor_still_does(tmp_path):
    repo = _repo(tmp_path / "repo")
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "two"], cwd=repo, check=True)
    res = await tool_bash({"command": "GIT_EDITOR='echo msg >' git commit --allow-empty"}, cwd=repo)
    assert res["exit_code"] == 0, res
    ok = await tool_bash({"command": "GIT_SEQUENCE_EDITOR=: git rebase -i HEAD~1"}, cwd=repo)
    assert ok["exit_code"] == 0 and "Successfully rebased" in ok["stderr"] + ok["stdout"], ok


@needs_bwrap
async def test_the_sandboxed_command_gets_the_environment_too(tmp_path):
    res = await tool_bash(
        {"command": "echo $GIT_EDITOR $PAGER $GIT_TERMINAL_PROMPT"}, cwd=tmp_path, sandbox=sandbox.build_argv(tmp_path)
    )
    assert res["stdout"].split() == ["true", "cat", "0"]


# ── a CA bundle outside the folders the sandbox shows ─────────────────

CA_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "SSL_CERT_DIR")


def _ca_env(monkeypatch, **named: str) -> None:
    for name in CA_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in named.items():
        monkeypatch.setenv(name, value)


def _ro_binds(argv: list[str]) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--ro-bind"]


def test_a_ca_bundle_the_environment_names_is_bound_read_only_after_the_home_tmpfs(tmp_path, monkeypatch):
    home, project, elsewhere = tmp_path / "home", tmp_path / "proj", tmp_path / "elsewhere"
    for d in (home, project, elsewhere):
        d.mkdir()
    corp, other = home / "corp-ca.pem", elsewhere / "ca.pem"
    corp.write_text("pem")
    other.write_text("pem")
    _ca_env(monkeypatch, SSL_CERT_FILE=str(corp), CURL_CA_BUNDLE=str(corp), REQUESTS_CA_BUNDLE=str(other))
    argv = sandbox.build_argv(project, home=home, bwrap="/usr/bin/bwrap")
    binds = _ro_binds(argv)
    assert (str(corp), str(corp)) in binds and (str(other), str(other)) in binds
    assert binds.count((str(corp), str(corp))) == 1  # named by two variables, bound once
    bind = next(i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == str(corp))
    assert bind > argv.index("--tmpfs", argv.index(str(home)) - 1)  # after the home is hidden


def test_only_the_files_the_variables_name_are_bound(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "proj"
    for d in (home, project, home / ".ssh", home / "certs"):
        d.mkdir(parents=True)
    (home / ".ssh" / "ca.pem").write_text("pem")
    (project / "ca.pem").write_text("pem")
    cases = {
        "SSL_CERT_FILE": str(home / ".ssh" / "ca.pem"),  # a secrets folder
        "CURL_CA_BUNDLE": str(project / "ca.pem"),  # already visible, and writable
        "REQUESTS_CA_BUNDLE": str(home / "certs"),  # a folder
        "NODE_EXTRA_CA_CERTS": str(home / "missing.pem"),
        "SSL_CERT_DIR": str(home / "certs"),
    }
    _ca_env(monkeypatch, **cases)
    binds = _ro_binds(sandbox.build_argv(project, home=home, bwrap="/usr/bin/bwrap"))
    assert not [b for b in binds if str(home) in b[0]]
    _ca_env(monkeypatch, SSL_CERT_FILE="relative/ca.pem")
    assert not [
        b for b in _ro_binds(sandbox.build_argv(project, home=home, bwrap="/usr/bin/bwrap")) if "ca.pem" in b[0]
    ]
    system = Path("/etc/hostname")
    if system.is_file():  # under /etc, which the sandbox shows anyway
        _ca_env(monkeypatch, SSL_CERT_FILE=str(system))
        assert (str(system), str(system)) not in _ro_binds(
            sandbox.build_argv(project, home=home, bwrap="/usr/bin/bwrap")
        )


@needs_bwrap
async def test_a_command_in_the_sandbox_reads_the_ca_bundle_the_environment_names(tmp_path, monkeypatch):
    elsewhere, project = tmp_path / "elsewhere", tmp_path / "proj"
    elsewhere.mkdir()
    project.mkdir()
    ca = elsewhere / "corp-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    _ca_env(monkeypatch, SSL_CERT_FILE=str(ca), CURL_CA_BUNDLE=str(ca))
    argv = sandbox.build_argv(project)
    res = await tool_bash(
        {"command": 'head -c 10 "$SSL_CERT_FILE"; echo x >> "$SSL_CERT_FILE"'}, cwd=project, sandbox=argv
    )
    assert res["stdout"] == "-----BEGIN" and "Read-only file system" in res["stderr"]
    assert ca.read_text() == "-----BEGIN CERTIFICATE-----\n"  # the host's file is untouched


# ── a cwd that is not there ───────────────────────────────────────────


async def test_a_missing_cwd_is_named_not_a_python_repr(tmp_path, clean_jobs):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    res = await tool_bash({"command": "ls", "cwd": "nonexistent"}, cwd=tmp_path)
    assert res == {"error": f"cwd {tmp_path / 'nonexistent'} does not exist (relative to the session directory)"}
    res = await tool_bash({"command": "ls", "cwd": "a.txt"}, cwd=tmp_path)
    assert res == {"error": f"cwd {tmp_path / 'a.txt'} is not a directory"}
    res = await tool_bash({"command": "ls", "cwd": "/nonexistent/abs"}, cwd=tmp_path)
    assert res == {"error": "cwd /nonexistent/abs does not exist (relative to the session directory)"}
    bg = await tool_bash({"command": "ls", "cwd": "nope", "background": True}, cwd=tmp_path, session_id="nocwd")
    assert "does not exist" in bg["error"] and jobs.list_jobs("nocwd") == []
    ok = await tool_bash({"command": "pwd", "cwd": "sub"}, cwd=tmp_path)
    assert ok["stdout"].strip() == str(tmp_path / "sub")


# ── a write that is refused ───────────────────────────────────────────


def _refuse_temp_files(monkeypatch) -> None:
    real_open = os.open

    def deny(path, *args, **kwargs):
        if ".k3tmp-" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("k3code.tools.os.open", deny)


async def test_a_refused_write_names_the_file_not_the_temp_file(tmp_path, monkeypatch, caplog):
    target = tmp_path / "ro" / "new.txt"
    existing = tmp_path / "old.txt"
    existing.write_text("old\n")
    _refuse_temp_files(monkeypatch)
    res = await tool_write({"path": str(target), "content": "x"}, cwd=tmp_path)
    assert res == {"error": f"permission denied: {target}"}
    res = await tool_edit({"path": str(existing), "old_string": "old", "new_string": "new"}, cwd=tmp_path)
    assert res == {"error": f"permission denied: {existing}"}
    assert existing.read_text() == "old\n" and not list(tmp_path.glob("**/.*k3tmp*"))


async def test_the_loop_answers_a_refused_write_without_a_traceback(tmp_path, monkeypatch, caplog):
    loop = AgentLoop(
        Router(build_chain([_Script([])], [["m"]]), max_retries=0),
        system_prompt="t",
        permission_mode="yolo",
        cwd=tmp_path,
    )
    _refuse_temp_files(monkeypatch)
    with caplog.at_level("ERROR"):
        res = await loop._execute_tool(ToolCall(id="w", name="write", arguments={"path": "f.txt", "content": "x"}))
    assert res == {"error": f"permission denied: {tmp_path / 'f.txt'}"} and not caplog.records


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
async def test_a_write_into_a_read_only_folder_is_refused_by_name(tmp_path):
    folder = tmp_path / "ro"
    folder.mkdir()
    folder.chmod(0o555)
    try:
        res = await tool_write({"path": "ro/new.txt", "content": "x"}, cwd=tmp_path)
    finally:
        folder.chmod(0o755)
    assert res == {"error": f"permission denied: {folder / 'new.txt'}"}


# ── read past the end of a big file ───────────────────────────────────


async def test_a_read_past_the_end_of_a_big_file_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr("k3code.tools.MAX_READ_BYTES", 1000)
    big = tmp_path / "big.log"
    big.write_text("".join(f"line {i:05d} of the log\n" for i in range(200)))
    assert big.stat().st_size > 1000
    past = format_tool_result(await tool_read({"path": str(big), "offset": 500, "limit": 3}, cwd=tmp_path))
    assert past == "[file has 200 lines; offset 500 is past the end]"
    last = format_tool_result(await tool_read({"path": str(big), "offset": 200, "limit": 3}, cwd=tmp_path))
    assert last.startswith("200\tline 00199 of the log")
    after = format_tool_result(await tool_read({"path": str(big), "offset": 201}, cwd=tmp_path))
    assert after == "[file has 200 lines; offset 201 is past the end]"


async def test_the_size_of_a_file_over_the_limit_is_given_with_a_decimal(tmp_path):
    mib = 1024 * 1024
    for size, shown in ((11_068_270, "10.6"), (10 * mib + 1, "10.1"), (25 * mib, "25.0")):
        big = tmp_path / f"big{size}.bin"
        with big.open("wb") as f:
            f.truncate(size)  # sparse
        res = await tool_read({"path": str(big)}, cwd=tmp_path)
        assert res["error"].startswith(f"File is {shown} MB (limit 10 MB): pass offset and limit"), res


# ── bash timeouts ─────────────────────────────────────────────────────


async def test_a_negative_timeout_is_refused_and_a_big_one_is_cut_in_seconds(tmp_path):
    res = await tool_bash({"command": "echo hi", "timeout": -5}, cwd=tmp_path)
    assert res == {"error": "timeout must be a positive number of seconds, got -5"}
    # 30000 may be milliseconds (Claude Code) or seconds: reading it as ms would kill a 1000 s build after 1 s, so it
    # stays seconds, capped, and the note says so
    cut = await tool_bash({"command": "echo hi", "timeout": 30000}, cwd=tmp_path)
    assert cut["stdout"] == "hi\n" and "timeout 30000 was cut to 600: it is in seconds" in cut["note"]
    assert (await tool_bash({"command": "echo hi", "timeout": 0}, cwd=tmp_path))["stdout"] == "hi\n"  # 0: the default


async def test_a_timeout_does_not_advise_more_time_for_a_command_that_waits_for_input(tmp_path):
    res = await tool_bash({"command": "sleep 5", "timeout": 0.3}, cwd=tmp_path)
    assert res["error"].startswith("Command timed out after 0.3s; retry with timeout up to 600 or background: true")
    assert "waits for input" in res["error"] and "stdin is closed" in res["error"]
