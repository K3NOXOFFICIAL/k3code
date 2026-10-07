"""Claude Code CLI provider: one stateless ``claude -p`` call per model turn.

For people who have a Claude Code login but no API key. Every turn k3code sends the whole
conversation again, so the call is stateless: no session is saved, no project files or memory are
read, no MCP servers or skills load, and **all of Claude Code's own tools are switched off**
(``--tools ""``). The model answers through a JSON schema (``{text, tool_calls}``) and k3code's own
loop executes the tools, so permissions, sandbox, journal and approvals still apply.

The subprocess runs in a private empty directory with the ``ANTHROPIC_*`` / ``OMNIROUTE_*``
environment removed (so it always uses the Claude Code login, never a relay) and never reads the
login files itself. Cost is the user's Claude plan, not an API bill.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pwd
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from typing import Any

from k3code.providers.base import Provider, ProviderError
from k3code.providers.types import Message, StreamEvent, ToolCall, ToolSpec, Usage

DEFAULT_COMMAND = "claude"
_SYSTEM_FALLBACK = "You are the model behind a coding agent. Answer only through the JSON output schema."

_PREAMBLE = (
    "You are the language-model backend of a coding agent called k3code. The environment your host reports to you "
    "(working directory, git state, date, memory, tool list) belongs to an unrelated scratch sandbox: ignore all of "
    "it. The user's project, its working directory and every file path are described only by the conversation and "
    "the tool results below. You have no tools of your own here; use only the tools listed below."
)

_FOOTER = (
    "Write the assistant's next turn. Answer ONLY through the JSON output schema: put prose for the user in "
    '"text"; to use tools, list the calls in "tool_calls" (independent calls may be listed together) and leave '
    '"text" empty. Never describe a tool call in prose instead of making it.'
)


def render_prompt(messages: list[Message], tools: list[ToolSpec]) -> tuple[str, str]:
    """Return ``(system, prompt)``: the system text and the transcript the CLI gets on stdin."""
    system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
    parts: list[str] = [_PREAMBLE]
    if tools:
        catalogue = [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]
        parts.append("## Tools you can call (JSON schema per tool)\n" + json.dumps(catalogue, ensure_ascii=False))
    parts.append("## Conversation so far")
    for m in messages:
        if m.role == "system":
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
    parts.append(_FOOTER)
    return system, "\n\n".join(parts)


def output_schema(tools: list[ToolSpec]) -> dict[str, Any]:
    """JSON schema of the reply: prose in ``text`` and, when tools exist, a ``tool_calls`` list."""
    props: dict[str, Any] = {"text": {"type": "string", "description": "Reply to the user; empty when calling tools."}}
    required = ["text"]
    if tools:
        props["tool_calls"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": [t.name for t in tools]},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
            },
        }
        required.append("tool_calls")
    return {"type": "object", "properties": props, "required": required}


def _real_home() -> str:
    """The login home, even when HOME points at a test or sandbox directory."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:  # pragma: no cover - no passwd entry
        return os.path.expanduser("~")


def _clean_env() -> dict[str, str]:
    drop = ("ANTHROPIC_", "OMNIROUTE_")
    env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
    env["HOME"] = _real_home()
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
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
    ) -> None:
        self.name = name
        self.base_url = ""  # nothing to probe over HTTP; the general internet probe covers reachability
        self.api_key = ""
        self.command = command
        self.timeout = timeout
        self.setting_sources = setting_sources
        self._max_parallel = max(1, max_parallel)
        self._sem: asyncio.Semaphore | None = None
        self._workdir: str | None = None

    def __repr__(self) -> str:
        return f"ClaudeCliProvider(name={self.name!r}, command={self.command!r})"

    async def aclose(self) -> None:
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    def _cwd(self) -> str:
        if not self._workdir or not os.path.isdir(self._workdir):
            self._workdir = tempfile.mkdtemp(prefix="k3code-claude-cli.")
        return self._workdir

    def _command_line(self, model: str, schema: dict[str, Any], system_file: str) -> list[str]:
        binary = shutil.which(self.command) or self.command
        return [
            binary,
            "-p",
            "--model",
            model,
            "--output-format",
            "json",
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
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]

    async def _run(self, argv: list[str], prompt: str) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd(),
                env=_clean_env(),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ProviderError(
                f"The Claude Code CLI ({self.command!r}) was not found on PATH. Install Claude Code and run "
                "`claude` once to log in, or remove this provider from the chain.",
                status_code=401,
            ) from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=self.timeout)
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 9)
            await proc.wait()
            raise ProviderError(f"claude -p timed out after {self.timeout:g}s", status_code=504) from exc
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 9)
            raise
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        *,
        max_tokens: int = 8192,  # not settable on the CLI; accepted for interface parity
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        system, prompt = render_prompt(messages, tools)
        schema = output_schema(tools)
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_parallel)
        system_file = os.path.join(self._cwd(), f"system-{uuid.uuid4().hex[:8]}.txt")
        with open(system_file, "w", encoding="utf-8") as fh:
            fh.write(system or _SYSTEM_FALLBACK)
        try:
            async with self._sem:
                rc, out, err = await self._run(self._command_line(model, schema, system_file), prompt)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(system_file)

        result = _last_json(out)
        if rc != 0 or result is None or result.get("is_error"):
            raise _error_from_result(result, err, rc)

        text, calls = _reply_from_result(result, tools)
        usage = _usage(result)
        if text:
            yield StreamEvent(type="text_delta", text=text)
        for call in calls:
            yield StreamEvent(type="tool_call", tool_call=call)
        final = Message(role="assistant", content=text or None, tool_calls=calls, usage=usage)
        yield StreamEvent(type="done", message=final, usage=usage)


def _last_json(out: str) -> dict[str, Any] | None:
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _reply_from_result(result: dict[str, Any], tools: list[ToolSpec]) -> tuple[str, list[ToolCall]]:
    structured = result.get("structured_output")
    if not isinstance(structured, dict):
        raw = (result.get("result") or "").strip()
        try:
            structured = json.loads(raw) if raw.startswith("{") else None
        except json.JSONDecodeError:
            structured = None
    if not isinstance(structured, dict):  # plain-text answer (no schema honoured): treat it as the reply
        return (result.get("result") or "").strip(), []
    text = str(structured.get("text") or "")
    known = {t.name for t in tools}
    calls: list[ToolCall] = []
    for item in structured.get("tool_calls") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        args = item.get("arguments")
        args = args if isinstance(args, dict) else {}
        name = str(item["name"])
        if known and name not in known:
            continue
        calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:12]}",
                name=name,
                arguments=args,
                raw_arguments=json.dumps(args, ensure_ascii=False),
            )
        )
    return text, calls


def _usage(result: dict[str, Any]) -> Usage:
    u = result.get("usage") or {}
    prompt = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return Usage(prompt_tokens=prompt, completion_tokens=int(u.get("output_tokens") or 0))
