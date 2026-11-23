"""Bug catcher: one scrubbed JSON row per failure in ``$K3CODE_HOME/bugs.jsonl``.

:func:`record` writes a row (credentials masked as ``/debug dump`` masks them, repeats rate-limited, the file capped).
:func:`install` feeds it from uncaught exceptions, thread exceptions and ERROR log records; :func:`install_loop`
from an asyncio loop. Importing this module changes nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from typing import Any

from k3code.paths import ensure_private_dir, private_file

try:
    import fcntl
except ImportError:  # Windows: no flock; rows are still written, unserialised
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

FILE = "bugs.jsonl"
#: Past this size the file keeps its newer half (lines), rewritten in place.
_MAX_BYTES = 2 * 1024 * 1024
#: The same fingerprint within this many seconds of its last written row is counted, not written.
_WINDOW = 60.0
_SUMMARY_CHARS = 300
_TRACEBACK_CHARS = 4096
_FIELD_CHARS = 500
#: Past this many fingerprints the expired ones are dropped from the rate-limit table.
_SEEN_MAX = 1024
#: The attribute that marks each wrapper and handler this module installs.
_MARK = "_k3_bugcatch"

#: Monotonic clock and ``{fp: [last written, suppressed since]}``: module attributes so tests can replace them.
_clock: Callable[[], float] = time.monotonic
_seen: dict[str, list[float]] = {}
_seen_lock = threading.Lock()
_guard = threading.local()

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_PATH = re.compile(r"(?:[A-Za-z]:\\|~/|/)[^\s:'\",;()\[\]]*")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{8,}\b")
_DIGITS = re.compile(r"\d+")


def _off() -> bool:
    return os.environ.get("K3CODE_BUGCATCH", "").strip().lower() == "off"


@contextlib.contextmanager
def _guarded() -> Iterator[bool]:
    """Yield True when this thread is already inside the catcher (a row being written, a chained hook running)."""
    if getattr(_guard, "active", False):
        yield True
        return
    _guard.active = True
    try:
        yield False
    finally:
        _guard.active = False


def normalise(message: str) -> str:
    """``message`` without what varies between two reports of one bug: quoted strings, paths, ids and numbers."""
    message = _QUOTED.sub("<s>", message)
    message = _PATH.sub("<path>", message)
    message = _UUID.sub("<id>", message)
    message = _HEX.sub("<hex>", message)
    return _DIGITS.sub("<n>", message)


def fingerprint(kind: str, exc_type: str, message: str, frame: str) -> str:
    """One bug's identity: its kind, exception type, normalised message and innermost k3code frame."""
    key = "\x1f".join((kind, exc_type, normalise(message), frame))
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


def _innermost_frame(exc: BaseException | None) -> str:
    """``module:function`` of the innermost k3code frame of ``exc``'s traceback, else of the caller's stack."""
    frames: list[tuple[str, str]] = []
    if exc is not None:
        frames = [(f.f_globals.get("__name__", ""), f.f_code.co_name) for f, _ in traceback.walk_tb(exc.__traceback__)]
        frames.reverse()
    else:
        frames = [(f.f_globals.get("__name__", ""), f.f_code.co_name) for f, _ in traceback.walk_stack(None)]
    for module, func in frames:
        if (module == "k3code" or module.startswith("k3code.")) and module != __name__:
            return f"{module}:{func}"
    return ""


def _secrets() -> list[str]:
    from k3code.debugdump import secret_values

    try:
        from k3code.config import load_config

        return secret_values(load_config())
    except Exception:  # noqa: BLE001 - a config that does not load must not cost the row
        logger.debug("bug row: config not loaded, masking env secrets only", exc_info=True)
        return secret_values(None)


def _scrub(text: str, secrets: list[str]) -> str:
    from k3code.debugdump import redact_text
    from k3code.redact import scrub_text, scrub_urls

    return scrub_text(scrub_urls(redact_text(text, secrets)), opaque=True)


def _field(value: Any, secrets: list[str], limit: int = _FIELD_CHARS) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _scrub(str(value), secrets)[:limit]


def _rate_limited(fp: str) -> int | None:
    """None when a row for ``fp`` is to be skipped (counted), else the repeats skipped since its last row."""
    now = _clock()
    with _seen_lock:
        entry = _seen.get(fp)
        if entry is not None and now - entry[0] < _WINDOW:
            entry[1] += 1
            return None
        if len(_seen) >= _SEEN_MAX:
            for key in [k for k, (last, _) in _seen.items() if now - last >= _WINDOW]:
                del _seen[key]
        _seen[fp] = [now, 0]
        return int(entry[1]) if entry is not None else 0


def _append(line: str) -> None:
    """Append ``line`` under an flock on the file itself; past the cap first keep the newer half, in place (the
    daemon, the TUI gateway and the CLI all hold this file: a replaced inode would lose their rows)."""
    from k3code.daemon import k3_home

    path = k3_home() / FILE
    ensure_private_dir(path.parent)
    fd = os.open(private_file(path), os.O_RDWR)
    with os.fdopen(fd, "r+b") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        if os.fstat(f.fileno()).st_size > _MAX_BYTES:
            f.seek(0)
            lines = f.read().splitlines(keepends=True)
            f.seek(0)
            f.write(b"".join(lines[len(lines) // 2 :]))
            f.truncate()
        f.seek(0, os.SEEK_END)
        f.write(line.encode("utf-8"))
        f.flush()


def record(kind: str, summary: str, *, exc: BaseException | None = None, **ctx: Any) -> None:
    """Append one bug row to ``$K3CODE_HOME/bugs.jsonl``. ``session``, ``tool``, ``provider`` and ``model`` in
    ``ctx`` get fields of their own; the rest goes to ``ctx``, each value scrubbed and cut. ``_frame`` names the
    failing ``module:function`` where there is no traceback to take it from. Never raises; ``K3CODE_BUGCATCH=off``
    makes it a no-op."""
    if _off():
        return
    with _guarded() as inside:
        if inside:
            return
        try:
            _ensure_handler()
            _record(kind, summary, exc, ctx)
        except Exception:  # noqa: BLE001 - the catcher must never fail the code that reports to it
            logger.debug("bug row not written", exc_info=True)


def _record(kind: str, summary: str, exc: BaseException | None, ctx: dict[str, Any]) -> None:
    frame = ctx.pop("_frame", None) or _innermost_frame(exc)
    exc_type = type(exc).__name__ if exc is not None else ""
    message = str(exc) if exc is not None else summary
    fp = fingerprint(kind, exc_type, message, frame)
    suppressed = _rate_limited(fp)
    if suppressed is None:
        return
    secrets = _secrets()
    tb = ""
    if exc is not None:
        tb = _scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), secrets)
        tb = tb[-_TRACEBACK_CHARS:]  # the end: the innermost frames and the exception line
    from k3code._version import __version__

    named = {key: _field(ctx.pop(key, None), secrets) for key in ("session", "tool", "provider", "model")}
    row: dict[str, Any] = {
        "ts": time.time(),
        "kind": _field(kind, secrets),
        "fp": fp,
        "summary": _scrub(summary or "", secrets)[:_SUMMARY_CHARS],
        "exc_type": exc_type,
        "traceback": tb,
        **named,
        "version": __version__,
        "ctx": {str(k)[:100]: _field(v, secrets) for k, v in ctx.items()},
    }
    if suppressed:
        row["suppressed"] = suppressed
    _append(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _summary(exc_type: type[BaseException], exc: BaseException | None) -> str:
    return f"{exc_type.__name__}: {exc}" if exc is not None and str(exc) else exc_type.__name__


def _ignored(exc_type: type[BaseException] | None) -> bool:
    return exc_type is None or issubclass(exc_type, (KeyboardInterrupt, SystemExit))


def _chain(previous: Callable[..., Any], *args: Any) -> None:
    """Run the hook ``install`` replaced with the guard set, so a hook that logs the failure adds no second row."""
    with _guarded():
        previous(*args)


class _Handler(logging.Handler):
    """An ERROR log record becomes a ``log_error`` row; its frame is the logger name and function."""

    def emit(self, rec: logging.LogRecord) -> None:
        try:
            exc = rec.exc_info[1] if rec.exc_info else None
            record("log_error", rec.getMessage(), exc=exc, _frame=f"{rec.name}:{rec.funcName}", logger=rec.name)
        except Exception:  # noqa: BLE001 - a bad format string must not fail the logging call
            logger.debug("log record not turned into a bug row", exc_info=True)


def _ensure_handler() -> None:
    """Put the ERROR handler on the root logger when it is missing (``basicConfig(force=True)`` strips it)."""
    root = logging.getLogger()
    if not any(getattr(h, _MARK, False) for h in root.handlers):
        handler = _Handler(logging.ERROR)
        setattr(handler, _MARK, True)
        root.addHandler(handler)


def install() -> None:
    """Feed :func:`record` from ``sys.excepthook``, ``threading.excepthook`` and ERROR records on the root logger.
    Idempotent per hook; each previous hook still runs. A second call re-adds the log handler if it was stripped."""
    if not getattr(sys.excepthook, _MARK, False):
        previous_sys = sys.excepthook

        def sys_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
            if not _ignored(exc_type):
                record("uncaught", _summary(exc_type, exc), exc=exc)
            _chain(previous_sys, exc_type, exc, tb)

        setattr(sys_hook, _MARK, True)
        sys.excepthook = sys_hook
    if not getattr(threading.excepthook, _MARK, False):
        previous_thread = threading.excepthook

        def thread_hook(args: Any) -> None:
            if not _ignored(args.exc_type):
                thread = getattr(args.thread, "name", None)
                record("thread", _summary(args.exc_type, args.exc_value), exc=args.exc_value, thread=thread)
            _chain(previous_thread, args)

        setattr(thread_hook, _MARK, True)
        threading.excepthook = thread_hook
    _ensure_handler()


def install_loop(loop: Any) -> None:
    """Feed :func:`record` from ``loop``'s exception handler; the previous one, or the loop's default, still runs
    (with the guard set: the default logs at ERROR, and that must not add a second row). Idempotent."""
    previous = loop.get_exception_handler()
    if getattr(previous, _MARK, False):
        return

    def handler(lp: Any, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if not (isinstance(exc, BaseException) and _ignored(type(exc))):
            exc = exc if isinstance(exc, BaseException) else None
            record("asyncio", str(context.get("message") or "asyncio failure"), exc=exc)
        if previous is not None:
            _chain(previous, lp, context)
        else:
            _chain(lp.default_exception_handler, context)

    setattr(handler, _MARK, True)
    loop.set_exception_handler(handler)
