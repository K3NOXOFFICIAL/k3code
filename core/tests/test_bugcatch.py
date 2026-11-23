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


# --- capture call sites --------------------------------------------------------------------------------------------


class _Caller:
    """A ModelCaller stand-in for the classifier: one reply (with its stop reason), an exception, or a delay."""

    def __init__(self, text: str = "", *, stop: str | None = None, raises: Exception | None = None, delay: float = 0):
        self.text, self.stop, self.raises, self.delay = text, stop, raises, delay

    async def complete(self, kind, messages, **kw):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        from k3code.routing.caller import CallResult
        from k3code.routing.tiers import Tier

        return CallResult(self.text, Tier.CHEAP, stop_reason=self.stop)


async def _classify(caller, **kw):
    from k3code.permissions.classifier import classify
    from k3code.providers.types import Message, ToolCall

    call = ToolCall(id="c1", name="bash", arguments={"command": "rm -rf build"})
    history = [Message(role="user", content="clean the build")]
    return await classify(caller, call, history, cwd="/tmp", session_id="s1", **kw)


async def test_classifier_timeout_writes_a_row():
    v = await _classify(_Caller(delay=2.0), timeout_s=0.05)
    assert v.failed
    (row,) = _rows()
    assert row["kind"] == "classifier_timeout" and row["session"] == "s1" and row["ctx"]["timeout_s"] == 0.05


async def test_classifier_provider_error_writes_a_row():
    from k3code.providers.base import ProviderError

    await _classify(_Caller(raises=ProviderError("upstream 502")))
    (row,) = _rows()
    assert row["kind"] == "classifier_error" and row["exc_type"] == "ProviderError" and row["session"] == "s1"


async def test_classifier_chain_exhausted_row_carries_the_last_reason():
    from k3code.errors import ChainExhausted

    await _classify(_Caller(raises=ChainExhausted("all failed", "rate_limit")))
    (row,) = _rows()
    assert row["kind"] == "classifier_error" and row["exc_type"] == "ChainExhausted"
    assert row["ctx"]["last_reason"] == "rate_limit"


@pytest.mark.parametrize("stop", ["max_tokens", "length"])
async def test_classifier_cut_off_reply_is_truncated_not_unreadable(stop):
    await _classify(_Caller("<think>long thoughts", stop=stop))
    (row,) = _rows()
    assert row["kind"] == "classifier_truncated"


async def test_classifier_garbage_is_unreadable_and_garbles_share_one_fp(_restore_hooks):
    await _classify(_Caller("sure, allow it 123", stop="end_turn"))
    _restore_hooks[0] += 61  # past the rate-limit window, so the second row is written
    await _classify(_Caller("no idea, block maybe? " + "x" * 300))
    first, second = _rows()
    assert first["kind"] == second["kind"] == "classifier_unreadable"
    assert first["fp"] == second["fp"]
    assert first["ctx"]["reply_chars"] == 18 and first["ctx"]["reply_head"] == "sure, allow it 123"
    assert len(second["ctx"]["reply_head"]) == 200


async def test_classifier_too_long_command_writes_no_row():
    from k3code.permissions.classifier import CALL_CHARS, classify
    from k3code.providers.types import ToolCall

    call = ToolCall(id="c1", name="bash", arguments={"command": "x" * (CALL_CHARS + 1)})
    v = await classify(_Caller(), call, [], cwd="/tmp")
    assert v.failed and _rows() == []


async def test_model_caller_carries_the_stop_reason():
    from k3code.config import Settings
    from k3code.providers.types import Message
    from k3code.routing.caller import ModelCaller

    class _Router:
        async def complete(self, messages, tools, *, max_tokens):
            return Message(role="assistant", content="{", stop_reason="max_tokens")

    class _Routers:
        def get(self, tier):
            return _Router()

    res = await ModelCaller(lambda: _Routers(), Settings(providers=[]), None).complete("title", [], session_id="s")
    assert res.stop_reason == "max_tokens"


async def test_a_provider_error_turn_writes_turn_provider_error(tmp_path, monkeypatch):
    import m1cmd_helpers as m1
    from k3code.router import Router, build_chain

    class _Down(m1.TextProvider):
        async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
            raise RuntimeError("provider exploded")
            yield  # makes this an async generator, like the real stream

    server, _ = m1.make_server(tmp_path, monkeypatch, ["done"])
    provider = _Down(["done"])
    server.providers = [provider]
    server.router = Router(build_chain([provider], [["m"]]), max_retries=0)
    server._oneshot_routers["default"] = server.router
    sid = await m1.new_session(server, tmp_path)
    await m1.submit_and_wait(server, "hi")
    rows = [r for r in _rows() if r["kind"] == "turn_provider_error"]
    assert len(rows) == 1 and rows[0]["session"] == sid
    assert rows[0]["exc_type"] == "ChainExhausted"  # max_retries=0: the one entry fails, the chain is spent


def test_install_survives_service_logging_setup():
    """The gateway and the daemon call basicConfig(force=True), which strips every root handler: install must run
    after it, so the real setup is run here rather than install alone."""
    from k3code import cli

    bugcatch.install()
    cli._configure_service_logging()
    assert any(getattr(h, bugcatch._MARK, False) for h in logging.getLogger().handlers)
