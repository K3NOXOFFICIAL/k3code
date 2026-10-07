"""Anthropic messages API provider (async httpx, streaming, tools)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from k3code.providers.base import Provider, ProviderError, request_headers, to_provider_error
from k3code.providers.types import Message, StreamEvent, ToolCall, ToolSpec, messages_to_anthropic

_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)


class AnthropicProvider(Provider):
    """Talks to the Anthropic messages API (also most Claude-compatible relays)."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str = "https://api.anthropic.com",
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT)
        self._owns_client = client is None

    def __repr__(self) -> str:  # never leak the key
        return f"AnthropicProvider(name={self.name!r}, base_url={self.base_url!r})"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self) -> str:
        return f"{self.base_url}/v1/messages"

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
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
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
            headers=request_headers("anthropic", self.api_key),
            json=self._payload(messages, tools, model, max_tokens=max_tokens, temperature=temperature),
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise to_provider_error(exc, kind="anthropic") from exc
        if response.status_code >= 400:
            error = await _error_from_response(response)
            await response.aclose()
            raise error
        try:
            content_parts: list[str] = []
            # tool index -> {"id", "name", "args"}
            tool_blocks: dict[int, dict[str, Any]] = {}
            usage_in = usage_out = 0
            async for line in response.aiter_lines():
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
                    usage_in = int(usage.get("input_tokens") or 0)
                elif etype == "message_delta":
                    usage = event.get("usage") or {}
                    usage_out = int(usage.get("output_tokens") or 0)
            from k3code.providers.types import Usage

            usage = Usage(prompt_tokens=usage_in, completion_tokens=usage_out)
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
            )
            yield StreamEvent(type="done", message=final, usage=usage)
        except httpx.HTTPError as exc:
            raise to_provider_error(exc, kind="anthropic") from exc
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
    return ProviderError(message=message, status_code=response.status_code, headers=dict(response.headers), body=body)


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
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    except json.JSONDecodeError:
        return {"_unparsed": raw}
