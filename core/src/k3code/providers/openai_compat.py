"""OpenAI chat-completions compatible provider (async httpx, streaming, tools)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from k3code.providers.base import Provider, ProviderError, request_headers, to_provider_error
from k3code.providers.effort import openai_effort
from k3code.providers.types import (
    Message,
    StreamEvent,
    ToolCall,
    ToolSpec,
    messages_to_openai,
    with_cache_breakpoint,
)

_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)


class OpenAICompatProvider(Provider):
    """Talks to any chat-completions endpoint (OpenAI, OmniRoute, vLLM, ...)."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        prompt_cache: str = "auto",
    ) -> None:
        self.name = name
        #: "on": Anthropic-style cache_control inside messages for Claude model ids (auto = off: plain OpenAI
        #: endpoints cache by themselves and some reject unknown fields)
        self.prompt_cache = prompt_cache == "on"
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT)
        self._owns_client = client is None

    def __repr__(self) -> str:  # never leak the key
        return f"OpenAICompatProvider(name={self.name!r}, base_url={self.base_url!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _payload(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        *,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        wire = messages_to_openai(messages)
        if self.prompt_cache and "claude" in model.lower():
            _add_cache_breakpoints(wire)
        payload: dict[str, Any] = {
            "model": model,
            "messages": wire,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if effort := openai_effort(model):
            payload["reasoning_effort"] = effort
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
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
            headers=request_headers("openai", self.api_key),
            json=self._payload(messages, tools, model, max_tokens=max_tokens, temperature=temperature),
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise to_provider_error(exc, kind="openai") from exc
        if response.status_code >= 400:
            error = await _error_from_response(response)
            await response.aclose()
            raise error
        try:
            tool_calls: dict[int, dict[str, Any]] = {}
            content_parts: list[str] = []
            usage = None
            complete = False  # [DONE] or a finish_reason arrived: the answer is whole
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    complete = True
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict) and chunk.get(
                    "error"
                ):  # also OpenRouter's error + finish_reason "error" shape
                    raise _inband_error(chunk)  # the upstream failed after answering 200
                choices = chunk.get("choices") or []
                if choices:
                    if choices[0].get("finish_reason"):
                        complete = True
                    delta = choices[0].get("delta") or {}
                    if text := delta.get("content"):
                        content_parts.append(text)
                        yield StreamEvent(type="text_delta", text=text)
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": []})
                        if tc_id := tc.get("id"):
                            slot["id"] = tc_id
                        fn = tc.get("function") or {}
                        if fn_name := fn.get("name"):
                            slot["name"] += fn_name
                        if args := fn.get("arguments"):
                            slot["args"].append(args)
                if chunk_usage := chunk.get("usage"):
                    usage = _parse_usage(chunk_usage)
            if not complete:
                # A proxy closing early or an upstream dying mid-answer ends the body cleanly: without this the
                # half sentence became a successful turn, was saved to history and never failed over.
                raise to_provider_error(
                    httpx.RemoteProtocolError(
                        "peer closed connection: stream ended before the completion finished (no [DONE]/finish_reason)"
                    ),
                    kind="openai",
                )
            final_calls: list[ToolCall] = []
            for idx in sorted(tool_calls):
                slot = tool_calls[idx]
                raw = "".join(slot["args"]) or None
                arguments = _parse_args(raw)
                final_calls.append(
                    ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"], arguments=arguments, raw_arguments=raw)
                )
            final = Message(
                role="assistant",
                content="".join(content_parts) or None,
                tool_calls=final_calls,
                usage=usage,
            )
            yield StreamEvent(type="done", message=final, usage=usage)
        except httpx.HTTPError as exc:
            raise to_provider_error(exc, kind="openai") from exc
        finally:
            await response.aclose()


async def _error_from_response(response: httpx.Response) -> ProviderError:
    try:
        text = await response.aread()
        body = json.loads(text) if text else {}
        if not isinstance(body, dict):
            body = {"message": str(body)}
    except Exception:
        body = {}
    message = _error_message(body) or f"HTTP {response.status_code}"
    headers = dict(response.headers)
    return ProviderError(message=message, status_code=response.status_code, headers=headers, body=body)


def _inband_error(chunk: dict[str, Any]) -> ProviderError:
    """An ``{"error": {...}}`` data chunk inside a 200 stream, as a ProviderError the classifier understands."""
    err = chunk.get("error")
    status = None
    if isinstance(err, dict):
        for key in ("status", "status_code", "code"):
            value = err.get(key)
            if isinstance(value, int) and 400 <= value < 600:
                status = value
                break
            if isinstance(value, str) and value.isdigit() and 400 <= int(value) < 600:
                status = int(value)
                break
    return ProviderError(
        message=_error_message(chunk) or "error in stream", status_code=status or 502, headers={}, body=chunk
    )


def _error_message(body: dict[str, Any]) -> str:
    err = body.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    if isinstance(err, str):
        return err
    for key in ("message", "detail"):
        if body.get(key):
            return str(body[key])
    return ""


def _add_cache_breakpoints(wire: list[dict[str, Any]]) -> None:
    """Two Anthropic-style breakpoints for a relay to Claude: the last leading system message and the newest one."""
    leading = 0
    while leading < len(wire) and wire[leading]["role"] == "system":
        leading += 1
    marks = {leading - 1, len(wire) - 1} - {-1}
    for i in marks:
        if isinstance(wire[i].get("content"), str):
            wire[i] = {**wire[i], "content": with_cache_breakpoint(wire[i]["content"])}


def _parse_usage(chunk_usage: dict[str, Any]) -> Any:
    from k3code.providers.types import Usage

    details = chunk_usage.get("prompt_tokens_details") or {}
    return Usage(
        prompt_tokens=int(chunk_usage.get("prompt_tokens") or 0),
        completion_tokens=int(chunk_usage.get("completion_tokens") or 0),
        # OpenAI reports cache hits in prompt_tokens_details; relays to Anthropic pass its own fields on
        cache_read_tokens=int(details.get("cached_tokens") or chunk_usage.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(
            details.get("cache_write_tokens") or chunk_usage.get("cache_creation_input_tokens") or 0
        ),
    )


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    except json.JSONDecodeError:
        return {"_unparsed": raw}
