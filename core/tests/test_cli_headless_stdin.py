"""`k3code -p` and piped stdin: ``echo text | k3code -p 'summarize'`` sends both, ``k3code -p -`` reads the prompt."""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from pathlib import Path

import click
import pytest

from k3code import cli

ARG = "PROMPT-ARG-91"
BODY = "STDIN-BODY-47"
# the fake provider answers a marker only when the model's conversation contains it
SCRIPT = [
    {"type": "text", "text": "[arg]", "match": ARG},
    {"type": "text", "text": "[body]", "match": BODY},
    {"type": "text", "text": "[second]", "match": "second line"},
]


def _run(tmp_path: Path, argv: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "k3home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "providers:\n- name: fake\n  kind: openai\n  base_url: http://fake\n  api_key_env: K3_TEST_KEY\n"
        "  models: {default: m}\npermission_mode: yolo\n"
    )
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    project = tmp_path / "proj"
    project.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("K3CODE_")}
    env |= {"K3CODE_HOME": str(home), "K3CODE_FAKE_PROVIDER": str(script), "K3_TEST_KEY": "x", "HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, "-c", "from k3code.cli import cli; cli()", *argv],
        cwd=project,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_piped_stdin_is_sent_with_the_prompt(tmp_path):
    res = _run(tmp_path, ["-p", ARG], f"{BODY}\n")
    assert res.returncode == 0, res.stderr
    assert res.stdout == "[arg][body]\n"


def test_dash_reads_the_whole_prompt_from_stdin(tmp_path):
    res = _run(tmp_path, ["-p", "-"], f"{BODY}\nsecond line\n")
    assert res.returncode == 0, res.stderr
    assert res.stdout == "[body][second]\n"  # the prompt is the stdin text, not the literal "-"


def test_dash_with_nothing_on_stdin_is_a_usage_error(tmp_path):
    res = _run(tmp_path, ["-p", "-"], "  \n")
    assert res.returncode == 2
    assert "no prompt on stdin" in res.stderr
    assert res.stdout == ""


class _Stdin:
    """Replaces ``sys.stdin`` with the read end of a real pipe (so ``select`` works) holding ``data``."""

    def __init__(self, monkeypatch, *, data: bytes | None = b"", close_writer: bool = True):
        r, w = os.pipe()
        if data:
            os.write(w, data)
        self.writer = w
        if close_writer:
            os.close(w)
        self.reader = os.fdopen(r, "r", encoding="utf-8")
        monkeypatch.setattr(sys, "stdin", self.reader)


def test_a_prompt_without_stdin_text_stays_as_typed(monkeypatch):
    _Stdin(monkeypatch, data=b"")
    assert cli._headless_prompt("fix it") == "fix it"
    assert cli._headless_prompt(None) is None


def test_stdin_text_follows_the_prompt_after_a_blank_line(monkeypatch):
    _Stdin(monkeypatch, data=b"line one\nline two\n\n")
    assert cli._headless_prompt("summarize") == "summarize\n\nline one\nline two"


def test_a_terminal_stdin_is_not_read_for_an_argument_prompt(monkeypatch):
    class Tty:
        closed = False

        def isatty(self):
            return True

        def read(self):
            raise AssertionError("must not block on the terminal")

    monkeypatch.setattr(sys, "stdin", Tty())
    assert cli._headless_prompt("fix it") == "fix it"


def test_an_idle_open_pipe_does_not_hang_the_run(monkeypatch, capsys):
    # a scheduler or harness that keeps stdin open and never writes: wait a moment, say so, and go on
    monkeypatch.setattr(cli, "_STDIN_WAIT_SECONDS", 0.1)
    stdin = _Stdin(monkeypatch, data=b"", close_writer=False)
    try:
        assert cli._headless_prompt("fix it") == "fix it"
    finally:
        os.close(stdin.writer)
        stdin.reader.close()
    assert "no input arrived on stdin" in capsys.readouterr().err


def test_stdin_redirected_from_a_file_is_read(monkeypatch, tmp_path):
    # epoll refuses a regular file; it never blocks, so it counts as ready
    f = tmp_path / "in.txt"
    f.write_text("from a file\n")
    with f.open(encoding="utf-8") as fh:
        monkeypatch.setattr(sys, "stdin", fh)
        assert cli._headless_prompt("summarize") == "summarize\n\nfrom a file"


def test_stdin_without_a_descriptor_is_read(monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("in memory\n"))
    assert cli._headless_prompt("summarize") == "summarize\n\nin memory"


def test_an_idle_open_pipe_above_fd_1023_does_not_hang_the_run(monkeypatch, capsys):
    # select() cannot poll a descriptor >= FD_SETSIZE; a long test run (or a daemon with many files open) gets there
    monkeypatch.setattr(cli, "_STDIN_WAIT_SECONDS", 0.1)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    high = 1100
    if hard != resource.RLIM_INFINITY and hard <= high:
        pytest.skip("descriptor limit too low to open fd 1100")
    if soft != resource.RLIM_INFINITY and soft <= high:
        resource.setrlimit(resource.RLIMIT_NOFILE, (high + 16, hard))
    r, w = os.pipe()
    os.dup2(r, high)
    os.close(r)
    reader = os.fdopen(high, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", reader)
    try:
        assert reader.fileno() > 1023
        assert cli._headless_prompt("fix it") == "fix it"
    finally:
        os.close(w)
        reader.close()
    assert "no input arrived on stdin" in capsys.readouterr().err


def test_a_slash_command_prompt_takes_no_stdin(monkeypatch):
    _Stdin(monkeypatch, data=b"not for the command\n")
    assert cli._headless_prompt("/stats") == "/stats"


def test_a_slash_command_read_from_stdin_still_runs_as_a_command(monkeypatch):
    _Stdin(monkeypatch, data=b"/stats\n")
    assert cli._headless_prompt("-") == "/stats"


def test_stdin_that_is_not_text_is_refused_not_a_traceback(monkeypatch):
    _Stdin(monkeypatch, data=b"\xff\xfe\x00 binary")
    with pytest.raises(click.ClickException, match="not UTF-8 text"):
        cli._headless_prompt("describe this")
