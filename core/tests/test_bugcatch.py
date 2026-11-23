"""k3code.bugcatch: scrubbed, rate-limited, size-capped bug rows and the hooks that feed them."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path

import pytest

from k3code import bugcatch


@pytest.fixture(autouse=True)
def _restore_hooks(monkeypatch):
    """Every test gets a fresh rate-limit table and clock, and leaves the root handlers, its level, the httpx level
    and both excepthooks as it found them."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    httpx_level = logging.getLogger("httpx").level
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(bugcatch, "_seen", {})
    now = [1000.0]
    monkeypatch.setattr(bugcatch, "_clock", lambda: now[0])
    yield now
    for h in root.handlers[:]:
        if h not in handlers:
            root.removeHandler(h)
    for h in handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(httpx_level)


def _rows() -> list[dict]:
    path = Path(os.environ["K3CODE_HOME"]) / "bugs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _raw() -> str:
    return (Path(os.environ["K3CODE_HOME"]) / "bugs.jsonl").read_text(encoding="utf-8")


def _strip_root() -> None:
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)


def test_record_masks_a_token_in_the_message():
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # 40 chars, a GitHub token shape
    bugcatch.record("test", f"upstream said {token} is bad")
    assert token not in _raw()
    (row,) = _rows()
    assert row["kind"] == "test" and "upstream said" in row["summary"]
    assert os.stat(Path(os.environ["K3CODE_HOME"]) / "bugs.jsonl").st_mode & 0o777 == 0o600


def test_configured_value_and_url_key_appear_nowhere(monkeypatch):
    home = Path(os.environ["K3CODE_HOME"])
    (home / "config.yaml").write_text(
        "providers:\n"
        "- {name: p, kind: openai, base_url: 'http://x/v1', api_key_env: BUGCATCH_PROVIDER_VAR, models: {default: m}}\n"
    )
    monkeypatch.setenv("BUGCATCH_PROVIDER_VAR", "plainword-horse-battery")
    url_key = "zq81mmvq"
    try:
        raise RuntimeError(f"auth failed with plainword-horse-battery at https://h.example/v1?api_key={url_key}")
    except RuntimeError as exc:
        bugcatch.record("test", str(exc), exc=exc, session="plainword-horse-battery", note=str(exc))
    raw = _raw()
    assert _rows(), "no row written"
    assert "plainword-horse-battery" not in raw
    assert url_key not in raw


def test_record_never_raises_on_an_unwritable_dir(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("K3CODE_HOME", str(blocker / "home"))
    bugcatch.record("test", "boom", exc=ValueError("boom"))
    assert not (blocker / "home").exists()


def test_off_switch_writes_nothing(monkeypatch):
    monkeypatch.setenv("K3CODE_BUGCATCH", "off")
    bugcatch.record("test", "boom")
    assert _rows() == []


def test_repeats_within_a_minute_are_counted_on_the_next_row(_restore_hooks):
    now = _restore_hooks
    for _ in range(4):
        bugcatch.record("test", "same failure 1")
    assert len(_rows()) == 1 and "suppressed" not in _rows()[0]
    now[0] += 61
    bugcatch.record("test", "same failure 2")
    rows = _rows()
    assert len(rows) == 2
    assert rows[1]["suppressed"] == 3
    assert rows[0]["fp"] == rows[1]["fp"]


def test_file_keeps_the_newer_half_at_the_cap_in_place(monkeypatch, _restore_hooks):
    now = _restore_hooks
    monkeypatch.setattr(bugcatch, "_MAX_BYTES", 4000)
    path = Path(os.environ["K3CODE_HOME"]) / "bugs.jsonl"
    bugcatch.record("test", "row first")
    inode = path.stat().st_ino
    for i in range(40):
        now[0] += 100
        bugcatch.record("test", f"row {'x' * 50} {i}")
    assert path.stat().st_size < 4000 + 1000
    assert path.stat().st_ino == inode
    rows = _rows()
    assert "row first" not in [r["summary"] for r in rows]
    assert rows[-1]["summary"].endswith(" 39")
    assert 0 < len(rows) < 40


def test_fp_is_stable_across_numbers_and_paths():
    a = bugcatch.fingerprint("test", "OSError", "read /home/a/x.txt failed after 3 tries at 0x7f00ab12 'foo'", "m:f")
    b = bugcatch.fingerprint(
        "test", "OSError", "read /tmp/other/y.log failed after 17 tries at 0xdeadbeef 'bar'", "m:f"
    )
    c = bugcatch.fingerprint("test", "OSError", "write /home/a/x.txt failed after 3 tries at 0x7f00ab12 'foo'", "m:f")
    d = bugcatch.fingerprint("test", "OSError", "read /home/a/x.txt failed after 3 tries at 0x7f00ab12 'foo'", "m:g")
    assert a == b
    assert a != c and a != d


def test_fp_ignores_ctx_and_uses_the_innermost_k3code_frame():
    def fail():
        raise ValueError("bad value 12")

    try:
        fail()
    except ValueError as exc:
        bugcatch.record("test", "x", exc=exc, note="one")
        bugcatch._seen.clear()
        bugcatch.record("test", "x", exc=exc, note="two")
    rows = _rows()
    assert rows[0]["fp"] == rows[1]["fp"]
    assert rows[0]["exc_type"] == "ValueError" and "bad value" in rows[0]["traceback"]
    assert rows[0]["ctx"] == {"note": "one"}


def test_install_is_idempotent_and_chains_the_old_excepthook():
    seen = []

    def old(t, v, tb):
        seen.append(v)

    sys.excepthook = old
    bugcatch.install()
    hook = sys.excepthook
    bugcatch.install()
    assert sys.excepthook is hook and hook is not old
    exc = RuntimeError("crashed 1")
    sys.excepthook(RuntimeError, exc, None)
    assert seen == [exc]
    (row,) = [r for r in _rows() if r["kind"] == "uncaught"]
    assert row["exc_type"] == "RuntimeError"
    assert sum(1 for h in logging.getLogger().handlers if getattr(h, bugcatch._MARK, False)) == 1


def test_install_chains_the_thread_excepthook():
    seen = []
    threading.excepthook = lambda args: seen.append(args.exc_value)
    bugcatch.install()
    t = threading.Thread(target=lambda: 1 / 0)
    t.start()
    t.join()
    assert len(seen) == 1 and isinstance(seen[0], ZeroDivisionError)
    assert [r["exc_type"] for r in _rows() if r["kind"] == "thread"] == ["ZeroDivisionError"]


def test_handler_records_error_and_ignores_lower_levels():
    bugcatch.install()
    log = logging.getLogger("k3code.sample")
    log.warning("just a warning")
    log.error("provider failed with code 500")
    rows = _rows()
    assert [r["kind"] for r in rows] == ["log_error"]
    assert rows[0]["summary"] == "provider failed with code 500"


def test_handler_is_re_added_after_root_handlers_are_stripped():
    bugcatch.install()
    _strip_root()
    bugcatch.install()
    logging.getLogger("k3code.sample").error("after strip")
    assert [r["summary"] for r in _rows()] == ["after strip"]
    _strip_root()
    bugcatch.record("test", "any row re-adds it")
    assert any(getattr(h, bugcatch._MARK, False) for h in logging.getLogger().handlers)


def test_asyncio_failure_gives_one_row():
    bugcatch.install()
    loop = asyncio.new_event_loop()
    try:
        bugcatch.install_loop(loop)
        bugcatch.install_loop(loop)
        loop.call_exception_handler({"message": "task blew up", "exception": KeyError("k")})
    finally:
        loop.close()
    assert [r["kind"] for r in _rows()] == ["asyncio"]
