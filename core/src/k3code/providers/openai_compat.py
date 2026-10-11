"""OpenAI chat-completions compatible provider (async httpx, streaming, tools)."""

from __future__ import annotations

import json
import logging
import uuid
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

logger = logging.getLogger(__name__)

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
        cache_ttl: str = "5m",
    ) -> None:
        self.name = name
        #: Anthropic-style cache_control inside messages, for Claude model ids only (plain OpenAI endpoints cache by
        #: themselves). "on" always sends it; "auto" sends it until the endpoint answers 400 to a request with it and
        #: accepts the same request without (some relays reject the field): it is dropped for the rest of the run.
        self.prompt_cache = prompt_cache in ("on", "auto")
        self._cache_probing = prompt_cache == "auto"
        self.cache_ttl = cache_ttl
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
            _add_cache_breakpoints(wire, self.cache_ttl, newest=bool(tools))
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

    async def _open(
        self, messages: list[Message], tools: list[ToolSpec], model: str, *, max_tokens: int, temperature: float | None
    ) -> httpx.Response:
        """The streaming response to the request, or the ProviderError of its HTTP status. With ``prompt_cache: auto`` a
        400 to a request that carried cache breakpoints is answered by the same request without them: if that works
        the endpoint does not take the field, and no later request carries it."""
        rejected: ProviderError | None = None
        while True:
            marked = self.prompt_cache and "claude" in model.lower()
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
            if 300 <= response.status_code < 400:  # the client does not follow redirects: http where https is needed
                where = response.headers.get("location", "")
                await response.aclose()
                raise ProviderError(
                    message=f"HTTP {response.status_code}: the endpoint redirects to {where or 'another address'}; "
                    "set the provider's base_url to the final address",
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body={},
                )
            if response.status_code < 400:
                if rejected is not None:
                    logger.warning(
                        "%s does not take prompt-cache breakpoints: sending Claude requests without", self.name
                    )
                return response
            error = await _error_from_response(response)
            await response.aclose()
            if rejected is None and response.status_code == 400 and marked and self._cache_probing:
                rejected, self.prompt_cache = error, False
                continue
            if rejected is not None:  # it failed without the breakpoints too: they were not the problem
                self.prompt_cache = True
                raise rejected
            raise error

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        *,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        response = await self._open(messages, tools, model, max_tokens=max_tokens, temperature=temperature)
        try:
            tool_calls: dict[int, dict[str, Any]] = {}
            current: dict[int, int] = {}  # wire index -> slot of the call being streamed on it
            content_parts: list[str] = []
            usage = None
            complete = False  # [DONE] or a finish_reason arrived: the answer is whole
            finish_reason: str | None = None
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
                if not isinstance(chunk, dict):  # `data: [1]` and the like: nothing to read, nothing to crash on
                    continue
                if chunk.get("error"):  # also OpenRouter's error + finish_reason "error" shape
                    raise _inband_error(chunk)  # the upstream failed after answering 200
                choices = chunk.get("choices") or []
                if choices:
                    if choices[0].get("finish_reason"):
                        complete = True
                        finish_reason = choices[0]["finish_reason"]
                    delta = choices[0].get("delta") or {}
                    if text := delta.get("content"):
                        content_parts.append(text)
                        yield StreamEvent(type="text_delta", text=text)
                    for tc in delta.get("tool_calls") or []:
                        wire = tc.get("index", 0)
                        tc_id = tc.get("id")
                        key = current.get(wire)
                        # a different id on the same index is another call: some servers number every parallel call 0
                        if key is None or (tc_id and tool_calls[key]["id"] and tc_id != tool_calls[key]["id"]):
                            key = max(tool_calls, default=-1) + 1
                            current[wire] = key
                            tool_calls[key] = {"id": "", "name": "", "args": []}
                        slot = tool_calls[key]
                        if tc_id:
                            slot["id"] = tc_id
                        fn = tc.get("function") or {}
                        if fn_name := fn.get("name"):
                            # the name arrives whole, in pieces, or repeated whole in every delta ("readread")
                            if not slot["name"] or fn_name.startswith(slot["name"]):
                                slot["name"] = fn_name
                            elif fn_name != slot["name"]:
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
                    ToolCall(
                        id=slot["id"] or f"call_{uuid.uuid4().hex[:12]}",
                        name=slot["name"],
                        arguments=arguments,
                        raw_arguments=raw,
                    )
                )
            final = Message(
                role="assistant",
                content="".join(content_parts) or None,
                tool_calls=final_calls,
                usage=usage,
                stop_reason="max_tokens" if finish_reason == "length" else finish_reason,
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
    named = isinstance(err, dict) and any(
        isinstance(err.get(key), str) and not err[key].isdigit() for key in ("code", "type")
    )
    return ProviderError(
        message=_error_message(chunk) or "error in stream",
        # a named error (insufficient_quota, invalid_api_key, rate_limit_exceeded) is classified by its name; a
        # made-up 502 sent it to the generic server-error retry path first
        status_code=status or (None if named else 502),
        headers={},
        body=chunk,
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


def _add_cache_breakpoints(wire: list[dict[str, Any]], ttl: str = "5m", *, newest: bool = True) -> None:
    """Two Anthropic-style breakpoints for a relay to Claude: the last leading system message and the newest one."""
    leading = 0
    while leading < len(wire) and wire[leading]["role"] == "system":
        leading += 1
    marks = {leading - 1, *({len(wire) - 1} if newest else set())} - {-1}  # a call with no tools never reads it back
    for i in marks:
        if isinstance(wire[i].get("content"), str):
            wire[i] = {**wire[i], "content": with_cache_breakpoint(wire[i]["content"], ttl)}


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
