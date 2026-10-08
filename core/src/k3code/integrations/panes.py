"""k3 panes (tuios) integration: agent-state reports, approvals through the Inbox, new panes.

Everything here talks to the tuios daemon over its verb socket (``$TUIOS_SOCKET``): one JSON object per
line, ``{"id":N,"verb":"set-agent-state","params":{...}}`` answered by ``{"id":N,"result":{...}}`` or
``{"id":N,"error":{"code","message"}}``. The verbs used are ``set-agent-state`` (the pane's state),
``request-approval`` (holds a permission prompt for the Inbox; blocks until answered) and
``start-agent`` (opens an agent in a new pane).

Nothing is imported or started unless the pane env is set: :class:`PaneLink` is a frame tap that watches the JSON-RPC
frames between a TUI and a gateway (in the stdio gateway and in the ``gateway --attach`` bridge, the two
processes that live inside the pane and so inherit ``TUIOS_*``). With no ``TUIOS_SOCKET`` / ``TUIOS_PANE_ID``
:meth:`PaneLink.from_env` returns ``None`` and nothing happens.

Inbox answers: tuios' Inbox protocol knows ``once``, ``always`` and ``deny``. They map to the gateway's
``once``, ``always`` and ``deny``; ``session`` (allow for this session only) exists only in the pane's own
prompt, which stays on screen until answered there.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import shlex
import shutil
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

HARNESS = "k3code"
#: gateway session state -> tuios agent state (``idle`` right after a turn reads ``done``, see PaneReporter.report)
STATE_MAP = {
    "working": "working",
    "needs_input": "needs_input",
    "idle": "idle",
    "completed": "done",
    "failed": "errored",
}
#: the Inbox's three decisions, by what the gateway calls them
INBOX_TO_GATEWAY = {"once": "once", "always": "always", "deny": "deny"}
_SUMMARY_MAX = 150  # tuios refuses a held summary over 160 bytes
_CALL_TIMEOUT = 5.0
_HOLD_TIMEOUT = 310.0

#: JSON-RPC methods a read-only pane may not send
READONLY_BLOCKED = frozenset({
    "prompt.submit", "prompt.background", "session.interrupt", "session.steer", "session.delete",
    "session.title", "session.control", "session.mode.cycle", "session.mode.set", "command.dispatch",
    "slash.exec", "config.set", "clipboard.paste", "image.attach", "image.attach_bytes", "file.attach",
    "pdf.attach", "session.branch_stored", "model.save_key", "model.disconnect", "subagent.interrupt",
})


_SERVER_TOKENS = ("status.update", "approval", "clarify", "sudo", "secret", "request.cancel", "pane.open", "open_pane")


def map_state(state: str) -> str | None:
    """Gateway session state -> tuios state, or ``None`` for states tuios has no word for."""
    return STATE_MAP.get(state)


def clean_summary(text: str, limit: int = _SUMMARY_MAX) -> str:
    """One line the Inbox can show whole: no control characters, collapsed whitespace, <= ``limit`` bytes."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f  ]+", " ", text)
    text = " ".join(text.split())
    if len(text.encode()) <= limit:
        return text
    return text.encode()[: limit - 3].decode(errors="ignore").rstrip() + "..."


class TuiosError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class TuiosSocket:
    """Blocking client for the tuios verb socket. One connection per call, so a held ``request-approval``
    never blocks a state report and closing the connection cancels the hold."""

    def __init__(self, path: str, pane_id: str, session: str, token: str = "") -> None:
        self.path = path
        self.pane_id = pane_id
        self.session = session
        self.token = token

    def _connect(self, timeout: float) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(self.path)
        return s

    @staticmethod
    def _roundtrip(s: socket.socket, fh: Any, req_id: int, verb: str, params: dict[str, Any]) -> Any:
        s.sendall((json.dumps({"id": req_id, "verb": verb, "params": params}) + "\n").encode())
        line = fh.readline()
        if not line:
            raise TuiosError("closed", "the tuios socket closed without an answer")
        resp = json.loads(line)
        err = resp.get("error")
        if err:
            raise TuiosError(str(err.get("code", "error")), str(err.get("message", "")))
        return resp.get("result")

    def call(self, verb: str, params: dict[str, Any], timeout: float = _CALL_TIMEOUT,
             conn_hook: Callable[[socket.socket], None] | None = None) -> Any:
        s = self._connect(timeout)
        try:
            if conn_hook is not None:
                conn_hook(s)
            with s.makefile("r", encoding="utf-8") as fh:
                n = 1
                if self.token:  # places this connection in its pane where the daemon cannot by pid
                    with contextlib.suppress(Exception):
                        self._roundtrip(s, fh, n, "pane-grants", {"pane_id": self.pane_id, "pane_token": self.token})
                    n += 1
                return self._roundtrip(s, fh, n, verb, params)
        finally:
            with contextlib.suppress(OSError):
                s.close()


class PaneReporter:
    """Reports the pane's agent state; ordered, off-thread, deduplicated, never raises."""

    def __init__(self, sock: TuiosSocket) -> None:
        self.sock = sock
        self._last: tuple[str, str, str] | None = None
        self._q: queue.Queue[tuple[str, str, str] | threading.Event] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def report(self, state: str, message: str = "", kind: str = "") -> bool:
        """Queue a report of a *gateway* state. Returns whether a report was queued (False: unmapped or unchanged)."""
        tuios = map_state(state)
        if tuios is None:
            return False
        if tuios == "idle" and self._last is not None and self._last[0] in ("working", "needs_input"):
            tuios = "done"  # a turn just ended (like Claude Code's Stop hook): tuios lists it as finished
        return self.report_tuios(tuios, message, kind)

    def report_tuios(self, state: str, message: str = "", kind: str = "") -> bool:
        item = (state, clean_summary(message, 120), kind if state == "needs_input" else "")
        with self._lock:
            if item == self._last:
                return False
            self._last = item
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="k3-panes-report", daemon=True)
                self._worker.start()
        self._q.put(item)
        return True

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until queued reports were sent (tests, shutdown)."""
        if self._worker is None:
            return
        done = threading.Event()
        self._q.put(done)
        done.wait(timeout)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if isinstance(item, threading.Event):
                item.set()
                continue
            state, message, kind = item
            params: dict[str, Any] = {"session": self.sock.session, "window": self.sock.pane_id, "state": state,
                                      "harness": HARNESS}
            if message:
                params["message"] = message
            if kind:
                params["kind"] = kind
            try:
                self.sock.call("set-agent-state", params)
            except Exception as e:  # noqa: BLE001 - a report that cannot be sent must never break the gateway
                logger.debug("set-agent-state failed: %s", e)


class _Hold:
    """One approval held in the Inbox; cancelling closes its connection (tuios sees ``caller_gone``)."""

    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        self.cancelled = False

    def attach(self, s: socket.socket) -> None:
        self.sock = s
        if self.cancelled:
            with contextlib.suppress(OSError):
                s.close()

    def cancel(self) -> None:
        self.cancelled = True
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                self.sock.close()


#: ``inject(request_id, method, result)``: resolve a gateway request that the Inbox answered
Inject = Callable[[str, str, dict[str, Any]], None]


def readonly_verdict(obj: Any) -> str | None:
    """For a read-only attach: the error frame to answer a client frame that would change the session with ("" to
    drop it silently: a response to a server request), or ``None`` to forward it."""
    if not isinstance(obj, dict):
        return None
    m = obj.get("method")
    if m in READONLY_BLOCKED or (m is None and "id" in obj):
        if m is None:
            return ""  # a response frame: drop silently
        return json.dumps({"jsonrpc": "2.0", "id": obj.get("id"),
                           "error": {"code": -32000, "message": "this pane is read-only"}})
    return None


class PaneLink:
    """Watches the frames of one TUI<->gateway connection and mirrors them into the pane's tuios state."""

    def __init__(self, sock: TuiosSocket, inject: Inject | None = None, *, readonly: bool = False,
                 env: Mapping[str, str] | None = None, sync: bool = False) -> None:
        self.sock = sock
        self.reporter = PaneReporter(sock)
        self.inject = inject
        self.readonly = readonly
        self.sync = sync  # run holds and spawns inline (tests); otherwise on threads
        self.env = dict(env) if env is not None else dict(os.environ)
        self._holds: dict[str, _Hold] = {}
        self._threads: list[threading.Thread] = []

    @classmethod
    def from_env(cls, inject: Inject | None = None, *, readonly: bool = False,
                 environ: Mapping[str, str] | None = None) -> PaneLink | None:
        env = os.environ if environ is None else environ
        path, pane = env.get("TUIOS_SOCKET", ""), env.get("TUIOS_PANE_ID", "")
        if not path or not pane:
            return None
        sock = TuiosSocket(path, pane, env.get("TUIOS_SESSION", ""), env.get("TUIOS_PANE_TOKEN", ""))
        return cls(sock, inject, readonly=readonly, env=env)

    # ── frames ────────────────────────────────────────────────────────

    def on_server_line(self, line: str) -> None:
        """A frame the gateway sent to the TUI."""
        if not any(t in line for t in _SERVER_TOKENS):  # most frames are streamed text: skip parsing them
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        method, params = obj.get("method"), obj.get("params") or {}
        try:
            if method == "event":
                self._on_event(str(params.get("type", "")), params.get("payload") or {})
            elif method == "approval" and "id" in obj:
                self._on_approval(str(obj["id"]), params)
            elif method in ("clarify", "sudo", "secret") and "id" in obj:
                self.reporter.report("needs_input", str(params.get("question") or "asks you a question"), "question")
            elif method == "request.cancel":
                self._cancel(str(params.get("id", "")))
            elif method is None and isinstance(obj.get("result"), dict) and obj["result"].get("open_pane"):
                self.open_pane(obj["result"]["open_pane"])
        except Exception:  # noqa: BLE001 - the tap must never break the connection
            logger.debug("pane tap failed on a server frame", exc_info=True)

    def on_client_line(self, line: str) -> str | None:
        """A frame the TUI sent to the gateway. Returns an error frame to send back instead when the pane is
        read-only and the request would change something (the caller then drops the original), else ``None``."""
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        if obj.get("method") is None and "id" in obj:  # the TUI answered a server request: stop any hold
            self._cancel(str(obj["id"]))
        return readonly_verdict(obj) if self.readonly else None

    def _on_event(self, etype: str, payload: dict[str, Any]) -> None:
        if etype == "status.update" and isinstance(payload.get("state"), str):
            self.reporter.report(payload["state"], str(payload.get("text") or ""))
        elif etype == "pane.open" and isinstance(payload, dict):
            self.open_pane(payload)

    # ── approvals ─────────────────────────────────────────────────────

    def _on_approval(self, req_id: str, params: dict[str, Any]) -> None:
        what = params.get("command") or params.get("description") or params.get("tool_name") or "approval"
        summary = clean_summary(str(what))
        self.reporter.report_tuios("needs_input", summary, "approval")
        if self.inject is None or self.readonly:
            return
        hold = _Hold()
        self._holds[req_id] = hold
        job = lambda: self._hold(req_id, hold, params, summary)  # noqa: E731
        if self.sync:
            self.reporter.flush()
            job()
        else:
            t = threading.Thread(target=job, name="k3-panes-approval", daemon=True)
            self._threads.append(t)
            t.start()

    def approval_params(self, params: dict[str, Any], summary: str) -> dict[str, Any]:
        options = [o for o in ("once", "always", "deny") if o in (params.get("choices") or ["once", "always", "deny"])]
        out: dict[str, Any] = {
            "session": self.sock.session, "window": self.sock.pane_id, "harness": HARNESS,
            "options": options, "summary": summary,
        }
        if params.get("tool_name"):
            out["tool"] = str(params["tool_name"])
        if params.get("command"):
            out["target"] = str(params["command"])[:2000]
        pattern = clean_summary(str(params.get("pattern") or ""), 120)
        if "always" in options:
            if pattern:
                out["always_scope"] = [pattern + " (k3code project rules)"]
            else:
                out["options"] = [o for o in options if o != "always"]
        return out

    def _hold(self, req_id: str, hold: _Hold, params: dict[str, Any], summary: str) -> None:
        # tuios only holds a pane that is already on needs_input/approval: send that report first.
        self.reporter.flush()
        try:
            res = self.sock.call("request-approval", self.approval_params(params, summary),
                                 timeout=_HOLD_TIMEOUT, conn_hook=hold.attach)
        except Exception as e:  # noqa: BLE001
            logger.debug("request-approval failed: %s", e)
            return
        finally:
            self._holds.pop(req_id, None)
        decision = INBOX_TO_GATEWAY.get(str((res or {}).get("decision") or ""))
        if decision is None or hold.cancelled:
            return  # no answer (viewed, timeout, handed back): the pane's own prompt decides
        result: dict[str, Any] = {"choice": decision}
        if decision == "deny":
            result["reason"] = str((res or {}).get("message") or "denied from the Inbox")
        if self.inject is not None:
            self.inject(req_id, "approval", result)
        self.reporter.report_tuios("working")

    def _cancel(self, req_id: str) -> None:
        hold = self._holds.pop(req_id, None)
        if hold is not None:
            hold.cancel()

    def join(self, timeout: float = 5.0) -> None:
        for t in self._threads:
            t.join(timeout)
        self.reporter.flush(timeout)

    # ── new panes ─────────────────────────────────────────────────────

    def open_pane(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Start ``k3code attach <session>`` (or ``k3code tail <subagent>``) in a new pane of this tuios session.

        Runs on a thread (start-agent waits for the new pane): the gateway's event loop calls this."""
        if k3code_argv(spec, self.env) is None:
            return None
        if self.sync:
            return self._start_agent(spec)
        threading.Thread(target=self._start_agent, args=(spec,), name="k3-panes-open", daemon=True).start()
        return None

    def _start_agent(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        argv = k3code_argv(spec, self.env)
        assert argv is not None
        params: dict[str, Any] = {
            "session": self.sock.session, "agent": " ".join(shlex.quote(a) for a in argv),
            "name": str(spec.get("name") or spec.get("session_id") or spec.get("subagent_id") or "k3code")[:40],
            "focus": bool(spec.get("focus", False)), "ready_timeout": 2000,
        }
        if spec.get("cwd"):
            params["cwd"] = str(spec["cwd"])
        env = {k: self.env[k] for k in ("K3CODE_HOME", "K3CODE_GATEWAY_SOCKET", "K3CODE_CONFIG_DIR") if k in self.env}
        if env:
            params["env"] = env
        try:
            return self.sock.call("start-agent", params, timeout=30.0)  # type: ignore[no-any-return]
        except Exception as e:  # noqa: BLE001
            logger.debug("start-agent failed: %s", e)
            return None


def k3code_argv(spec: Mapping[str, Any], env: Mapping[str, str] | None = None) -> list[str] | None:
    """The command a new pane runs for an ``open_pane`` spec."""
    env = os.environ if env is None else env
    exe = shutil.which("k3code", path=env.get("PATH")) if env.get("PATH") else shutil.which("k3code")
    base = [exe] if exe else [sys.executable, "-m", "k3code.cli"]
    if spec.get("session_id"):
        cmd = [*base, "attach", str(spec["session_id"])]
        if spec.get("readonly"):
            cmd.append("--readonly")
        return cmd
    if spec.get("subagent_id"):
        return [*base, "tail", str(spec["subagent_id"])]
    return None


def open_pane_spec(session_id: str, *, name: str = "", readonly: bool = False, cwd: str = "") -> dict[str, Any]:
    spec: dict[str, Any] = {"session_id": session_id, "readonly": readonly}
    if name:
        spec["name"] = name
    if cwd:
        spec["cwd"] = cwd
    return spec
