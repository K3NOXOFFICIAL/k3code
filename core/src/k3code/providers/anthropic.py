"""Anthropic messages API provider (async httpx, streaming, tools)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from k3code.providers.base import Provider, ProviderError, error_from_http, request_headers, to_provider_error
from k3code.providers.effort import anthropic_effort, usable_temperature
from k3code.providers.stall import StreamWatchdog
from k3code.providers.types import (
    Message,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
    cache_marker,
    leading_system_parts,
    messages_to_anthropic,
    with_cache_breakpoint,
)

_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)


#: Anthropic reports cache reads and cache writes apart from input_tokens; they are prompt tokens too.
_INPUT_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


class AnthropicProvider(Provider):
    """Talks to the Anthropic messages API (also most Claude-compatible relays)."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str = "https://api.anthropic.com",
        api_key: str,
        keyless: bool = False,
        client: httpx.AsyncClient | None = None,
        prompt_cache: str = "auto",
        cache_ttl: str = "5m",
        first_event_timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> None:
        self.name = name
        #: stall limits in seconds (see stall.py); None = the defaults for the model
        self._first_event_timeout = first_event_timeout
        self._idle_timeout = idle_timeout
        #: auto and on both mark cache breakpoints here (the native API supports them); off sends none
        self.prompt_cache = prompt_cache != "off"
        self.cache_ttl = cache_ttl
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.keyless = keyless  # api_key_env "": the endpoint takes no key, send no key header
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT)
        self._owns_client = client is None

    def __repr__(self) -> str:  # never leak the key
        return f"AnthropicProvider(name={self.name!r}, base_url={self.base_url!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self) -> str:
        return f"{self.base_url}/v1/messages"

    @property
    def _write_multiple(self) -> float | None:
        """A cache write against the base input price: the 1-hour entry costs 2x (None = the 5-minute 1.25x)."""
        return 2.0 if self.prompt_cache and self.cache_ttl == "1h" else None

    def _payload(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        *,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        system, rest = messages_to_anthropic(messages)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": rest,
        }
        if system:
            parts = leading_system_parts(messages)
            # two system messages (the text agents of one type share, then the part that is theirs) stay two blocks, so
            # the shared one can be cached by itself: one block made every sibling's request a different cache entry
            payload["system"] = [{"type": "text", "text": p} for p in parts] if len(parts) > 1 else system
        if (temperature := usable_temperature(model, temperature)) is not None:
            payload["temperature"] = temperature
        if effort := anthropic_effort(model):
            payload["output_config"] = {"effort": effort}
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        if self.prompt_cache:
            _add_cache_breakpoints(payload, self.cache_ttl)
        return payload

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        *,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        request = self._client.build_request(
            "POST",
            self._url(),
            headers=request_headers("anthropic", self.api_key, keyless=self.keyless),
            json=self._payload(messages, tools, model, max_tokens=max_tokens, temperature=temperature),
        )
        watchdog = StreamWatchdog.for_model(
            "anthropic", model, first_event=self._first_event_timeout, idle=self._idle_timeout
        )
        try:
            response = await watchdog.guard(self._client.send(request, stream=True))
        except httpx.HTTPError as exc:
            raise to_provider_error(exc, kind="anthropic") from exc
        if response.status_code >= 400:
            error = await _error_from_response(response)
            await response.aclose()
            raise error
        usage_in = usage_out = cache_read = cache_creation = 0

        try:
            content_parts: list[str] = []
            # tool index -> {"id", "name", "args"}
            tool_blocks: dict[int, dict[str, Any]] = {}
            stop_reason: str | None = None  # message_delta.delta.stop_reason: end_turn, tool_use, max_tokens, ...
            complete = False  # message_stop arrived
            async for line in watchdog.lines(response):
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "error":
                    raise _inband_error(event)  # overloaded_error etc. after a 200
                if etype != "ping":  # a ping is the relay's keepalive, not the model's progress
                    watchdog.progress()
                if etype == "message_stop":
                    complete = True
                if etype == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        tool_blocks[event.get("index", 0)] = {
                            "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                            "name": block.get("name") or "",
                            "args": [],
                        }
                elif etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            content_parts.append(text)
                            yield StreamEvent(type="text_delta", text=text)
                    elif delta.get("type") == "input_json_delta":
                        slot = tool_blocks.get(event.get("index", 0))
                        if slot is not None and (partial := delta.get("partial_json")):
                            slot["args"].append(partial)
                elif etype == "message_start":
                    message = event.get("message") or {}
                    usage = message.get("usage") or {}
                    # prompt tokens = every input token the model read, cache hits and cache writes included, the same
                    # definition as the claude-cli provider and the OpenAI-compatible one (whose count already has them)
                    usage_in = sum(int(usage.get(k) or 0) for k in _INPUT_USAGE_KEYS)
                    cache_read = int(usage.get("cache_read_input_tokens") or 0)
                    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
                    if usage_in:
                        # the prompt is counted (and billed) from here on, whatever becomes of the stream: the consumer
                        # charges this if the attempt is cut short (a dropped connection, /stop)
                        yield StreamEvent(
                            type="partial_usage",
                            usage=Usage(
                                prompt_tokens=usage_in,
                                cache_read_tokens=cache_read,
                                cache_creation_tokens=cache_creation,
                                cache_write_multiple=self._write_multiple,
                            ),
                        )
                elif etype == "message_delta":
                    usage = event.get("usage") or {}
                    usage_out = int(usage.get("output_tokens") or 0)
                    # relays and newer API versions finalize the input counts here: they only ever grow the tally
                    usage_in = max(usage_in, sum(int(usage.get(k) or 0) for k in _INPUT_USAGE_KEYS))
                    cache_read = max(cache_read, int(usage.get("cache_read_input_tokens") or 0))
                    cache_creation = max(cache_creation, int(usage.get("cache_creation_input_tokens") or 0))
                    stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
            if not complete:
                raise to_provider_error(
                    httpx.RemoteProtocolError("peer closed connection: stream ended before message_stop"),
                    kind="anthropic",
                )
            usage = Usage(
                prompt_tokens=usage_in,
                completion_tokens=usage_out,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                cache_write_multiple=self._write_multiple,
            )
            final_calls = [
                ToolCall(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=_parse_args("".join(slot["args"])),
                    raw_arguments="".join(slot["args"]) or None,
                )
                for _idx, slot in sorted(tool_blocks.items())
            ]
            final = Message(
                role="assistant",
                content="".join(content_parts) or None,
                tool_calls=final_calls,
                usage=usage,
                stop_reason=stop_reason,
            )
            yield StreamEvent(type="done", message=final, usage=usage)
        except httpx.HTTPError as exc:
            raise to_provider_error(exc, kind="anthropic") from exc
        finally:
            await response.aclose()


def _add_cache_breakpoints(payload: dict[str, Any], ttl: str = "5m") -> None:
    """Mark three prompt-cache breakpoints (the API allows four): the system prompt, the last tool definition, and the
    last block of the newest message. The last one moves forward every call, so each request reads the prefix the
    request before it wrote instead of paying for the whole conversation again on every tool-loop step."""
    marker = cache_marker(ttl)
    system = payload.get("system")
    if isinstance(system, list):  # shared block, then the part of this one request: a breakpoint after each
        payload["system"] = [{**block, "cache_control": marker} for block in system]
    elif system:
        payload["system"] = [{"type": "text", "text": system, "cache_control": marker}]
    if payload.get("tools"):
        payload["tools"][-1] = {**payload["tools"][-1], "cache_control": marker}
    messages = payload["messages"]
    # A call without tools (title, classifier, judge, compaction summary...) is asked once: the newest message is never
    # read back, so marking it only added the 25% cache-write surcharge to its whole input.
    if messages and payload.get("tools"):
        for i in range(len(messages) - 1, -1, -1):  # a newest message with nothing to carry it (an empty tool result)
            marked = with_cache_breakpoint(messages[i]["content"], ttl)  # hands the breakpoint to the one before
            if marked is not messages[i]["content"]:
                messages[i] = {**messages[i], "content": marked}
                break


async def _error_from_response(response: httpx.Response) -> ProviderError:
    return error_from_http(await response.aread(), response.status_code, dict(response.headers), _error_message)


_INBAND_STATUS = {
    "overloaded_error": 529,
    "rate_limit_error": 429,
    "api_error": 500,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "invalid_request_error": 400,
}


def _inband_error(event: dict[str, Any]) -> ProviderError:
    """An ``event: error`` inside a 200 stream, as a ProviderError the classifier understands."""
    err = event.get("error") if isinstance(event.get("error"), dict) else {}
    status = _INBAND_STATUS.get(str(err.get("type") or ""), 500)
    message = str(err.get("message") or "error in stream")
    return ProviderError(message=message, status_code=status, headers={}, body=event)


def _error_message(body: dict[str, Any]) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "")
    for key in ("message", "detail"):
        if body.get(key):
            return str(body[key])
    return ""


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw, strict=False)  # a raw newline or tab inside a string is a model slip, not a truncation
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    except json.JSONDecodeError:
        return {"_unparsed": raw}
