"""Claude Code CLI provider: ``claude -p`` processes that mirror k3code's conversation.

For people who have a Claude Code login but no API key. No Claude Code session is saved, no project files or memory
are read, no MCP servers or skills load, and **all of Claude Code's own tools are switched off**
(``--tools ""``). The model answers in plain text and asks for tools with one final
``<tool_calls>[...]</tool_calls>`` block; k3code's own loop executes them, so permissions, sandbox,
journal and approvals still apply. (``--json-schema`` was tried first: it forces a second model turn and
the model then writes its real answer outside the schema and a one-line summary inside it.)

The reply streams: ``--output-format stream-json --verbose --include-partial-messages`` prints one JSON event per
line, and the ``text_delta`` events reach the user as they arrive (with ``--output-format json`` the whole reply came
at once after 7-11 s of "thinking"). The final ``result`` event carries the full text, usage and error fields and
decides the ``done`` message, exactly as the single JSON object did before. The ``<tool_calls>`` block never reaches
the screen: a tail that could start it is held back, and nothing after it is streamed (see ``_StreamGate``).

Persistent sessions (``persistent: true``, the default): a process started with ``--input-format stream-json`` stays
alive between model steps and gets one user message per step, so the CLI's start (~1.7 s) is paid once and the prompt
cache covers the whole earlier conversation instead of only the system prefix. k3code's message list stays
authoritative: a process only MIRRORS a prefix of it. Processes are pooled by everything fixed at their start (model,
effort, thinking budget, system text, tool catalogue) and record the messages they have covered (what they were sent
plus the reply they gave). A step reuses an idle process whose covered list is a strict prefix of the request and sends
only the new messages (tool results, user input, later system notes); anything else (compaction, elision, a retry, a
failover answer, resume) is a prefix mismatch and starts a fresh process with the full render, so correctness never
depends on a match. Any failure kills the process and drops it from the pool. Claude Code's own auto-compaction is off
in these processes (``DISABLE_AUTO_COMPACT``): k3code compacts its own list. ``persistent: false`` keeps one stateless
call per step, the whole conversation rendered each time.

The subprocess runs in a private empty directory with the ``ANTHROPIC_*`` / ``OMNIROUTE_*``
environment removed (so it always uses the Claude Code login, never a relay) and never reads the
login files itself. Cost is the user's Claude plan, not an API bill.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import pwd
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from k3code.paths import GATEWAY_ENV_VARS
from k3code.providers import effort
from k3code.providers.base import Provider, ProviderError
from k3code.providers.types import INVALID_TOOL_CALL, Message, StreamEvent, ToolCall, ToolSpec, Usage

DEFAULT_COMMAND = "claude"
_SYSTEM_FALLBACK = "You are the model behind a coding agent."
_TOOL_BLOCK = re.compile(r"<tool_calls>\s*(.*?)\s*</tool_calls>", re.DOTALL)
_TOOL_TAG = "<tool_calls>"
_READ_CHUNK = 1 << 16
_EXIT = "k3code.exit"  # the pseudo-event ``_events`` ends with: return code and stderr of the finished process

_PREAMBLE = (
    "You are the language-model backend of a coding agent called k3code. The environment your host reports to you "
    "(working directory, git state, date, memory, tool list) belongs to an unrelated scratch sandbox: ignore all of "
    "it. The user's project, its working directory and every file path are described only by the conversation and "
    "the tool results below. You have no tools of your own here; use only the tools listed below."
)

_FOOTER_PLAIN = "Write the assistant's next turn as plain text for the user."
_FOOTER_TOOLS = (
    "Write the assistant's next turn as plain text for the user. To call tools, finish your reply with ONE block "
    '<tool_calls>[{"name": "<tool>", "arguments": {...}}, ...]</tool_calls> holding valid JSON (independent calls '
    "may be listed together) and write nothing after it. Use only the tools listed above, and never describe a tool "
    "call in prose instead of making it. If you need no tool, answer normally without the block."
)


_FOOTER_DELTA_TOOLS = (
    "Write the assistant's next turn as before: plain text, and to call tools end with ONE "
    '<tool_calls>[{"name": ..., "arguments": {...}}]</tool_calls> block and nothing after it; without the block if '
    "you need no tool."
)
_ARGS_CLIP = 200


def render_prompt(messages: list[Message], tools: list[ToolSpec]) -> tuple[str, str]:
    """Return ``(system, prompt)``: the system text and the transcript the CLI gets on stdin.

    Only the leading system messages form the system text. A later one (the loop guard's note) stays at its place in
    the transcript as a ``<system-reminder>``: joined into the system text it changed the CLI's system prompt from that
    call on, so the cached prefix missed for the rest of the turn (as in ``messages_to_anthropic``).
    """
    leading = 0
    while leading < len(messages) and messages[leading].role == "system":
        leading += 1
    system = "\n\n".join(m.content for m in messages[:leading] if m.content)
    parts: list[str] = [_PREAMBLE]
    if tools:
        catalogue = [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]
        parts.append("## Tools you can call (JSON schema per tool)\n" + json.dumps(catalogue, ensure_ascii=False))
    parts.append("## Conversation so far")
    for m in messages[leading:]:
        if m.role == "system":
            if m.content:
                parts.append(f"<system-reminder>\n{m.content}\n</system-reminder>")
            continue
        if m.role == "user":
            parts.append(f"[user]\n{m.content or ''}")
        elif m.role == "assistant":
            block = f"[assistant]\n{m.content or ''}".rstrip()
            for tc in m.tool_calls:
                args = tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False)
                block += f"\n(called tool {tc.name} id={tc.id} arguments={args})"
            parts.append(block)
        elif m.role == "tool":
            label = f"tool result id={m.tool_call_id or '?'}" + (f" tool={m.name}" if m.name else "")
            parts.append(f"[{label}]\n{m.content or ''}")
    parts.append(_FOOTER_TOOLS if tools else _FOOTER_PLAIN)
    return system, "\n\n".join(parts)


def _leading_system(messages: list[Message]) -> int:
    n = 0
    while n < len(messages) and messages[n].role == "system":
        n += 1
    return n


def render_delta(new: list[Message], last_calls: list[ToolCall], tools: list[ToolSpec]) -> str:
    """The messages a persistent process has not seen yet, as its next user message.

    The model in the process wrote its tool calls without ids, so each result is labelled by its position and call
    (``[tool result 1/2: bash {"command": "ls"}]``) instead of by k3code's id.
    """
    by_id = {tc.id: (i, tc) for i, tc in enumerate(last_calls, 1)}
    parts: list[str] = []
    for m in new:
        if m.role == "system":
            if m.content:
                parts.append(f"<system-reminder>\n{m.content}\n</system-reminder>")
        elif m.role == "user":
            parts.append(f"[user]\n{m.content or ''}")
        elif m.role == "tool":
            hit = by_id.get(m.tool_call_id or "")
            if hit:
                i, tc = hit
                args = json.dumps(tc.arguments, ensure_ascii=False)
                args = args if len(args) <= _ARGS_CLIP else args[:_ARGS_CLIP] + "..."
                label = f"tool result {i}/{len(last_calls)}: {tc.name} {args}"
            else:
                label = "tool result" + (f": {m.name}" if m.name else "")
            parts.append(f"[{label}]\n{m.content or ''}")
    parts.append(_FOOTER_DELTA_TOOLS if tools else _FOOTER_PLAIN)
    return "\n\n".join(parts)


def _fingerprint(m: Message) -> tuple[Any, ...]:
    """What a covered message must equal for a process to continue from it. Normalised like the gateway's session
    store (None and "" are the same content, id and name), so a reply that went through storage still matches."""
    calls = tuple((tc.id, tc.name, json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)) for tc in m.tool_calls)
    return (m.role, m.content or "", calls, m.tool_call_id or None, m.name or None)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(eq=False)
class _Session:
    """One live persistent ``claude -p`` process and the k3code messages it has covered."""

    proc: asyncio.subprocess.Process
    key: tuple[Any, ...]
    system_file: str
    stderr: asyncio.Future[bytes]
    covered: list[tuple[Any, ...]] = field(default_factory=list)
    last_calls: list[ToolCall] = field(default_factory=list)
    buf: bytes = b""
    busy: bool = True
    last_used: float = field(default_factory=time.monotonic)
    cost: float = 0.0  # the process's running total_cost_usd so far
    timer: asyncio.TimerHandle | None = None  # the idle-reap timer while the process waits for its next turn
    dropped: bool = False


def _real_home() -> str:
    """The login home, even when HOME points at a test or sandbox directory."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:  # pragma: no cover - no passwd entry
        return os.path.expanduser("~")


def _clean_env(thinking_tokens: int | None = 0) -> dict[str, str]:
    drop = ("ANTHROPIC_", "OMNIROUTE_", "MAX_THINKING_TOKENS", *GATEWAY_ENV_VARS)  # Claude Code runs its own tools
    env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
    env["HOME"] = _real_home()
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if thinking_tokens is not None:
        env["MAX_THINKING_TOKENS"] = str(max(0, int(thinking_tokens)))
    return env


def _error_from_result(result: dict[str, Any] | None, stderr: str, rc: int | None) -> ProviderError:
    """Map a failed CLI call to a ``ProviderError`` the router can classify."""
    text = ((result or {}).get("result") or "").strip() or stderr.strip()[-400:] or f"claude exited with code {rc}"
    low = text.lower()
    status = (result or {}).get("api_error_status")
    status = status if isinstance(status, int) else None
    if status is None:
        if "not logged in" in low or "/login" in low or "invalid api key" in low or "authentication" in low:
            status = 401
        elif "limit reached" in low or "usage limit" in low or "rate limit" in low or "quota" in low:
            status = 429
        elif "overloaded" in low:
            status = 529
        else:
            status = 502
    return ProviderError(message=text, status_code=status, body={"error": {"message": text}})


class ClaudeCliProvider(Provider):
    """Runs the ``claude`` binary once per turn (see the module docstring)."""

    def __init__(
        self,
        *,
        name: str,
        command: str = DEFAULT_COMMAND,
        timeout: float = 300.0,
        max_parallel: int = 2,
        setting_sources: str = "project",
        thinking_tokens: int | None = 0,
        thinking_models: tuple[str, ...] | list[str] = ("haiku",),
        effort: str | None = None,
        persistent: bool = True,
        max_sessions: int = 4,
        idle_seconds: float = 600.0,
    ) -> None:
        self.name = name
        self.base_url = ""  # nothing to probe over HTTP; the general internet probe covers reachability
        self.api_key = ""
        self.command = command
        self.timeout = timeout
        self.setting_sources = setting_sources
        self.thinking_tokens = thinking_tokens
        self.thinking_models = tuple(m.lower() for m in thinking_models)
        self.effort = effort  # --effort when /effort sets none for the turn
        self._max_parallel = max(1, max_parallel)
        self._sem: asyncio.Semaphore | None = None
        self._workdir: str | None = None
        self.persistent = persistent
        self.max_sessions = max(1, max_sessions)
        self.idle_seconds = idle_seconds
        self._pool: list[_Session] = []
        self._spawning = 0  # pool slots reserved by checkouts that are still evicting or spawning
        self._reaping: set[asyncio.Task[None]] = set()  # background reaps of idle-expired processes

    def __repr__(self) -> str:
        return f"ClaudeCliProvider(name={self.name!r}, command={self.command!r})"

    async def aclose(self) -> None:
        await self._drop(*self._pool)
        if self._reaping:
            await asyncio.gather(*self._reaping, return_exceptions=True)
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    def _cwd(self) -> str:
        if not self._workdir or not os.path.isdir(self._workdir):
            self._workdir = tempfile.mkdtemp(prefix="k3code-claude-cli.")
        return self._workdir

    def _command_line(
        self, model: str, system_file: str, *, persistent: bool = False, level: str | None = None
    ) -> list[str]:
        binary = shutil.which(self.command) or self.command
        argv = [
            binary,
            "-p",
            *(["--input-format", "stream-json"] if persistent else []),
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--verbose",  # stream-json in print mode requires it
            "--include-partial-messages",  # the text_delta events; without it only whole messages are printed
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--setting-sources",
            self.setting_sources,
            "--tools",
            "",
            "--system-prompt-file",
            system_file,
        ]
        if level := level or self._effort():
            argv += ["--effort", level]
        return argv

    def _effort(self) -> str | None:
        return effort.current() or self.effort  # /effort for this turn, else the provider entry's default

    def _thinking_for(self, model: str) -> int | None:
        """MAX_THINKING_TOKENS for this model: ``thinking_tokens`` for the models named in ``thinking_models`` (Haiku:
        the cheap tier, where hidden thinking cost ~20x the output of the task), Claude Code's own default otherwise
        (the strong tier plans, reviews and advises with its thinking intact)."""
        return self.thinking_tokens if any(m in model.lower() for m in self.thinking_models) else None

    async def _spawn_proc(self, argv: list[str], env: dict[str, str]) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd(),
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ProviderError(
                f"The Claude Code CLI ({self.command!r}) was not found on PATH. Install Claude Code and run "
                "`claude` once to log in, or remove this provider from the chain.",
                status_code=401,
            ) from exc

    async def _events(self, argv: list[str], prompt: str, model: str = "") -> AsyncIterator[dict[str, Any]]:
        """Run the CLI and yield each JSON event of its stdout as it is printed, then one ``_EXIT`` pseudo-event.

        ``timeout`` bounds the whole call, not each read. stdin is fed and stderr drained by their own tasks, so a full
        pipe on either side cannot stall the stdout reader. Whatever ends the call early (timeout, cancel, the consumer
        closing the stream) kills the process group and reaps it: a zombie and its pipe transports otherwise linger.
        stdout is split into lines here, not by ``readline``: a StreamReader line is capped at 64 KiB, and the
        ``result`` event carries the whole reply on one line.
        """
        proc = await self._spawn_proc(argv, _clean_env(self._thinking_for(model)))
        assert proc.stdin and proc.stdout and proc.stderr
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        feeder = asyncio.ensure_future(_feed(proc.stdin, prompt.encode()))
        stderr = asyncio.ensure_future(proc.stderr.read())
        try:
            try:
                buf = b""
                while chunk := await asyncio.wait_for(proc.stdout.read(_READ_CHUNK), deadline - loop.time()):
                    *lines, buf = (buf + chunk).split(b"\n")
                    for line in lines:
                        if event := _json_line(line):
                            yield event
                if event := _json_line(buf):  # a last line without a newline
                    yield event
                rc = await asyncio.wait_for(proc.wait(), max(0.0, deadline - loop.time()))
                err = await asyncio.wait_for(stderr, max(0.0, deadline - loop.time()))
            except TimeoutError as exc:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, 9)
                await proc.wait()
                raise ProviderError(f"claude -p timed out after {self.timeout:g}s", status_code=504) from exc
            yield {"type": _EXIT, "rc": rc or 0, "stderr": err.decode("utf-8", "replace")}
        finally:
            feeder.cancel()
            stderr.cancel()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, 9)
                # Shielded so a cancel cannot skip the reap, bounded so a stuck process cannot hold it up.
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(proc.wait()), 5)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        *,
        max_tokens: int = 8192,  # not settable on the CLI; accepted for interface parity
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_parallel)
        gate = _StreamGate(hold_tool_block=bool(tools))
        result: dict[str, Any] | None = None
        rc, err = 0, ""
        async with self._sem:
            sess: _Session | None = None
            if self.persistent:
                await self._reap()
                sess, prompt, covered = await self._checkout(messages, tools, model)
            events = self._oneshot_events(messages, tools, model) if sess is None else self._turn(sess, prompt)
            ok = False
            try:
                async with contextlib.aclosing(events):  # closed at once when the consumer stops early
                    async for event in events:
                        kind = event.get("type")
                        if kind == "stream_event":
                            if piece := gate.feed(_text_delta(event)):
                                yield StreamEvent(type="text_delta", text=piece)
                        elif kind == "result":
                            result = event
                        elif kind == _EXIT:
                            rc, err = event["rc"], event["stderr"]
                if rc != 0 or result is None or result.get("is_error"):
                    raise _error_from_result(result, err, rc)  # after streamed text the router sends "reset" first
                text, calls = _reply_from_result(result, tools)
                reuse = sess is not None and _mirrors_reply(result, tools)
                usage = _usage(result)
                final = Message(role="assistant", content=text or None, tool_calls=calls, usage=usage)
                if sess is not None:
                    total = usage.cost_usd
                    if total is not None:  # total_cost_usd is the process's running total: record this call's part
                        usage.cost_usd, sess.cost = max(0.0, total - sess.cost), total
                    sess.covered = [*covered, _fingerprint(final)]
                    sess.last_calls = list(calls)
                    sess.last_used = time.monotonic()
                    if reuse:
                        sess.busy = False
                        self._arm_idle_timer(sess)
                ok = True
            finally:
                # error, crash, timeout, cancel, a rejected reply, or a reply k3code keeps differently from the
                # process's own copy (prose around tool blocks, invalid blocks): never reuse it
                if sess is not None and not (ok and reuse):
                    await self._drop(sess)

        if rest := gate.rest(text):
            yield StreamEvent(type="text_delta", text=rest)
        for call in calls:
            yield StreamEvent(type="tool_call", tool_call=call)
        yield StreamEvent(type="done", message=final, usage=usage)

    async def _oneshot_events(
        self, messages: list[Message], tools: list[ToolSpec], model: str
    ) -> AsyncIterator[dict[str, Any]]:
        """The stateless path: the whole conversation to a fresh process that exits after its reply."""
        system, prompt = render_prompt(messages, tools)
        system_file = self._write_system(system)
        try:
            events = self._events(self._command_line(model, system_file), prompt, model)
            async with contextlib.aclosing(events):
                async for event in events:
                    yield event
        finally:
            with contextlib.suppress(OSError):
                os.unlink(system_file)

    def _write_system(self, system: str) -> str:
        path = os.path.join(self._cwd(), f"system-{uuid.uuid4().hex[:8]}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(system or _SYSTEM_FALLBACK)
        return path

    # ── persistent sessions ──

    async def _checkout(
        self, messages: list[Message], tools: list[ToolSpec], model: str
    ) -> tuple[_Session | None, str, list[tuple[Any, ...]]]:
        """An idle process that has covered a prefix of ``messages`` and the delta for it, else a new process and the
        full render; ``None`` when the pool is full of busy processes (the caller then runs a one-shot call)."""
        leading = _leading_system(messages)
        system, prompt = render_prompt(messages, tools)
        catalogue = json.dumps([[t.name, t.description, t.parameters] for t in tools], sort_keys=True)
        level, thinking = self._effort(), self._thinking_for(model)
        key = (model, level, thinking, _digest(system), _digest(catalogue))
        body = messages[leading:]
        fps = [_fingerprint(m) for m in body]
        best: _Session | None = None
        for s in self._pool:
            n = len(s.covered)
            if s.busy or s.key != key or s.proc.returncode is not None or not 0 < n < len(fps):
                continue
            if best is not None and n <= len(best.covered):
                continue  # the longest covered prefix wins
            if fps[:n] == s.covered and all(m.role != "assistant" for m in body[n:]):
                best = s
        if best is not None:
            best.busy = True
            self._disarm_idle_timer(best)
            return best, render_delta(body[len(best.covered) :], best.last_calls, tools), fps
        # A full render starts this conversation over: idle processes that began with the same first message are
        # superseded (an edited or compacted history) and would only hold a slot until they idled out.
        doomed = [s for s in self._pool if not s.busy and fps and s.key == key and s.covered[:1] == fps[:1]]
        for s in doomed:
            self._detach(s)
        # Everything up to the reservation below is synchronous, so concurrent checkouts see each other's slots.
        if len(self._pool) + self._spawning >= self.max_sessions:
            idle = [s for s in self._pool if not s.busy]
            if not idle:
                await self._drop(*doomed)
                return None, "", fps
            victim = min(idle, key=lambda s: s.last_used)  # least recently used
            self._detach(victim)
            doomed.append(victim)
        self._spawning += 1
        try:
            await self._drop(*doomed)
            system_file = self._write_system(system)
            env = _clean_env(thinking)
            env["DISABLE_AUTO_COMPACT"] = "1"  # k3code compacts its own message list; the mirror must not diverge
            try:
                cmd = self._command_line(model, system_file, persistent=True, level=level)
                proc = await self._spawn_proc(cmd, env)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(system_file)
                raise
            assert proc.stderr
            stderr = asyncio.ensure_future(proc.stderr.read())
            sess = _Session(proc=proc, key=key, system_file=system_file, stderr=stderr)
            self._pool.append(sess)
        finally:
            self._spawning -= 1
        return sess, prompt, fps

    async def _turn(self, sess: _Session, text: str) -> AsyncIterator[dict[str, Any]]:
        """Send one user message to a live process and yield its events up to and including ``result``. A process
        that exits first ends with an ``_EXIT`` event (its code and stderr); ``timeout`` bounds the whole step."""
        proc = sess.proc
        assert proc.stdin and proc.stdout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        line = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        try:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):  # an exited process shows up as EOF
                proc.stdin.write((json.dumps(line, ensure_ascii=False) + "\n").encode())
                await asyncio.wait_for(proc.stdin.drain(), deadline - loop.time())
            while True:
                while b"\n" not in sess.buf:
                    chunk = await asyncio.wait_for(proc.stdout.read(_READ_CHUNK), deadline - loop.time())
                    if not chunk:
                        rc = await asyncio.wait_for(proc.wait(), max(0.0, deadline - loop.time()))
                        err = await asyncio.wait_for(sess.stderr, max(0.0, deadline - loop.time()))
                        yield {"type": _EXIT, "rc": rc or 1, "stderr": err.decode("utf-8", "replace")}
                        return
                    sess.buf += chunk
                raw, sess.buf = sess.buf.split(b"\n", 1)
                if event := _json_line(raw):
                    yield event
                    if event.get("type") == "result":
                        return
        except TimeoutError as exc:
            raise ProviderError(f"claude -p timed out after {self.timeout:g}s", status_code=504) from exc

    async def _reap(self) -> None:
        """Drop idle processes that have exited or idled longer than ``idle_seconds`` (the idle timer normally gets
        there first; this catches exits and a loop that was too busy to run the timer)."""
        now = time.monotonic()
        stale = [
            s
            for s in self._pool
            if not s.busy and (s.proc.returncode is not None or now - s.last_used > self.idle_seconds)
        ]
        await self._drop(*stale)

    def _arm_idle_timer(self, sess: _Session) -> None:
        """Reap ``sess`` after ``idle_seconds`` without a next turn, even when no further call ever comes."""
        self._disarm_idle_timer(sess)
        sess.timer = asyncio.get_running_loop().call_later(max(0.0, self.idle_seconds), self._idle_expired, sess)

    @staticmethod
    def _disarm_idle_timer(sess: _Session) -> None:
        if sess.timer is not None:
            sess.timer.cancel()
            sess.timer = None

    def _idle_expired(self, sess: _Session) -> None:
        sess.timer = None
        if sess.busy or sess.dropped:
            return
        self._detach(sess)  # out of the pool and killed now; the reap itself runs as a task
        task = asyncio.get_running_loop().create_task(self._drop(sess))
        self._reaping.add(task)
        task.add_done_callback(self._reaping.discard)

    def _detach(self, sess: _Session) -> None:
        """The synchronous half of ``_drop``: out of the pool, never handed out again, process group killed."""
        if sess.dropped:
            return
        sess.dropped = True
        sess.busy = True
        self._disarm_idle_timer(sess)
        with contextlib.suppress(ValueError):
            self._pool.remove(sess)
        if sess.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(sess.proc.pid, 9)

    async def _drop(self, *sessions: _Session) -> None:
        """Remove processes from the pool, kill their process groups and reap them. All are killed before the first
        wait; each wait is shielded so a cancel cannot skip a reap and bounded so a stuck process cannot hold it up.
        A cancel that lands meanwhile is re-raised once every process is reaped, so the caller stops instead of
        going on to spawn a replacement."""
        for sess in sessions:
            self._detach(sess)
        cancelled: asyncio.CancelledError | None = None
        for sess in sessions:
            try:
                await asyncio.wait_for(asyncio.shield(sess.proc.wait()), 5)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:  # TimeoutError: a stuck process is left to the kernel after SIGKILL
                pass
            sess.stderr.cancel()
            with contextlib.suppress(OSError):
                os.unlink(sess.system_file)
        if cancelled is not None:
            raise cancelled


async def _feed(stdin: asyncio.StreamWriter, data: bytes) -> None:
    """Write the prompt and close stdin; a CLI that exits without reading it is reported by its exit code instead."""
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stdin.write(data)
        await stdin.drain()
        stdin.close()


def _json_line(line: bytes) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith(b"{"):
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _text_delta(event: dict[str, Any]) -> str:
    """The text of a ``stream_event`` carrying a ``text_delta``; "" for every other event (thinking and signature
    deltas, block and message boundaries)."""
    inner = event.get("event")
    delta = inner.get("delta") if isinstance(inner, dict) else None
    if not isinstance(delta, dict) or inner.get("type") != "content_block_delta" or delta.get("type") != "text_delta":
        return ""
    return str(delta.get("text") or "")


class _StreamGate:
    """Decides which streamed text may reach the user before the ``result`` event settles the reply.

    The ``done`` content is what ``_reply_from_result`` makes of the full text: stripped, and with tools cut at the
    first ``<tool_calls>`` block. Every streamed piece must keep the streamed text a prefix of it, so the gate drops
    leading whitespace, holds trailing whitespace back (it may be the end of the reply), holds back a tail that could
    be the start of ``<tool_calls>`` (``<tool_c`` must never flash on screen), and streams nothing once the tag has
    begun. ``rest`` then gives what the final content has beyond the streamed text (a held tail that turned out to be
    text, or the text around an empty block), so the screen ends up showing exactly the stored message.
    """

    def __init__(self, *, hold_tool_block: bool) -> None:
        self.hold_tool_block = hold_tool_block
        self.pending = ""  # received, not yet streamed
        self.sent: list[str] = []
        self.closed = False

    def feed(self, text: str) -> str:
        if self.closed or not text:
            return ""
        pending = self.pending + text
        if not self.sent:
            pending = pending.lstrip()
        hold = 0
        if self.hold_tool_block:
            cut = pending.find(_TOOL_TAG)
            if cut >= 0:
                self.closed = True
                pending = pending[:cut]
            else:
                hold = next((k for k in range(len(_TOOL_TAG) - 1, 0, -1) if pending.endswith(_TOOL_TAG[:k])), 0)
        out = pending[: len(pending) - hold].rstrip()
        self.pending = pending[len(out) :]
        if out:
            self.sent.append(out)
        return out

    def rest(self, final: str) -> str:
        sent = "".join(self.sent)
        return final[len(sent) :] if final.startswith(sent) else ""


def _reply_from_result(result: dict[str, Any], tools: list[ToolSpec]) -> tuple[str, list[ToolCall]]:
    """Split the model's plain-text reply into (text for the user, tool calls from every block, in order).

    Cheap models follow the "ONE final block" rule loosely: Haiku 4.5 splits its calls over several blocks
    ("write ... now let me run it: <tool_calls>bash"), so keeping only the last block silently dropped the write and the
    task stalled and escalated. All blocks count, in order. When calls were made, only the text before the first block
    is kept: what follows was written before the tools ran ("The script has been created. It prints 49") and is
    invented, not a result.
    """
    raw = str(result.get("result") or "")
    if not tools:
        return raw.strip(), []
    matches = list(_TOOL_BLOCK.finditer(raw))
    if not matches:
        if "<tool_calls>" in raw:  # opened but never closed: the reply was cut off
            raise ProviderError("The model started a <tool_calls> block but did not finish it.", status_code=502)
        return raw.strip(), []
    calls = [c for m in matches for c in _parse_tool_calls(m.group(1))]
    if not calls:
        return _TOOL_BLOCK.sub("", raw).strip(), []
    return raw[: matches[0].start()].strip(), calls


def _mirrors_reply(result: dict[str, Any], tools: list[ToolSpec]) -> bool:
    """Whether the process's own copy of its reply (the raw ``result`` text it keeps in its history) is what k3code
    keeps: the text before the first tool block, then the blocks, each of which parsed to at least one call, with
    nothing but whitespace between or after them. Anything else (prose after a block, an invalid block k3code
    strips) means k3code's history now differs from the process's, so the process must not be reused."""
    raw = str(result.get("result") or "")
    if not tools:
        return True  # the whole text is kept
    blocks = list(_TOOL_BLOCK.finditer(raw))
    if not blocks:
        return True
    if not all(_parse_tool_calls(b.group(1)) for b in blocks):
        return False
    gaps = [raw[a.end() : b.start()] for a, b in zip(blocks, blocks[1:], strict=False)] + [raw[blocks[-1].end() :]]
    return all(not g.strip() for g in gaps)


_NOT_ARGUMENTS = frozenset({"name", "tool", "id", "type", "arguments", "parameters", "input", "args"})


def _call_arguments(item: dict[str, Any]) -> dict[str, Any]:
    """The arguments of one call object: ``arguments`` (asked for), its common aliases, or - for models that flatten
    them next to the name (``{"name": "bash", "command": "ls"}``) - every other key."""
    for key in ("arguments", "parameters", "input", "args"):
        value = item.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):  # an OpenAI-style JSON string
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {k: v for k, v in item.items() if k not in _NOT_ARGUMENTS}


_PY_LITERALS = {"True": "true", "False": "false", "None": "null"}
_WORD = re.compile(r"[A-Za-z_]+")
_INVALID_BODY_CHARS = 300


def _repair_json(body: str) -> str:
    """``body`` with the slips models make in JSON fixed: a comma before ``]``/``}``, ``True``/``False``/``None``
    outside strings, and a missing final ``]`` when the array is otherwise complete. Anything else is left as it is."""
    out: list[str] = []
    stack: list[str] = []

    def drop_trailing_comma() -> None:
        k = len(out) - 1
        while k >= 0 and not out[k].strip():
            k -= 1
        if k >= 0 and out[k] == ",":
            del out[k]

    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            j = i + 1
            while j < n and body[j] != '"':
                j += 2 if body[j] == "\\" else 1
            out.append(body[i : j + 1])
            i = j + 1
            continue
        if ch in "[{":
            stack.append(ch)
        elif ch in "]}":
            if stack:
                stack.pop()
            drop_trailing_comma()
        elif m := _WORD.match(body, i):
            out.append(_PY_LITERALS.get(m.group(), m.group()))
            i = m.end()
            continue
        out.append(ch)
        i += 1
    if stack == ["["]:
        drop_trailing_comma()
        out.append("]")
    return "".join(out)


def _parse_tool_calls(body: str) -> list[ToolCall]:
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body.strip())  # tolerate a fenced block
    try:
        items = json.loads(body)
    except json.JSONDecodeError as exc:
        try:
            items = json.loads(_repair_json(body))
        except json.JSONDecodeError:
            # not a provider failure: a retry would regenerate the whole reply. The agent loop tells the model.
            detail = {"error": str(exc), "body": body[:_INVALID_BODY_CHARS]}
            return [
                ToolCall(
                    id=f"call_{uuid.uuid4().hex[:12]}",
                    name=INVALID_TOOL_CALL,
                    arguments=detail,
                    raw_arguments=json.dumps(detail, ensure_ascii=False),
                )
            ]
    if isinstance(items, dict):
        items = items.get("tool_calls") or [items]
    calls: list[ToolCall] = []
    for item in items if isinstance(items, list) else []:
        name = item.get("name") or item.get("tool") if isinstance(item, dict) else None
        if not name:
            continue
        args = _call_arguments(item)
        calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:12]}",
                name=str(name),  # an unknown name is passed on: the agent loop answers "unknown tool"
                arguments=args,
                raw_arguments=json.dumps(args, ensure_ascii=False),
            )
        )
    return calls


def _usage(result: dict[str, Any]) -> Usage:
    u = result.get("usage") or {}
    prompt = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    cost = result.get("total_cost_usd")  # Claude Code's own list-price figure (a plan is not billed this)
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=int(u.get("output_tokens") or 0),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(u.get("cache_creation_input_tokens") or 0),
    )
