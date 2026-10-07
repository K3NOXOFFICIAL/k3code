"""Agent loop: system prompt + messages → router → tool calls → repeat."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from k3code.permissions import PermissionMode, check_permission
from k3code.providers.types import Message, StreamEvent, ToolCall
from k3code.router import Router, RouterEvent
from k3code.tools import build_registry

logger = logging.getLogger(__name__)


class AgentLoop:
    """Runs the conversation loop with tool execution."""

    def __init__(
        self,
        router: Router,
        *,
        system_prompt: str,
        max_turns: int = 20,
        permission_mode: str = "ask",
        headless: bool = True,
        on_event: Callable[[RouterEvent], None] | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.router = router
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.permission_mode = PermissionMode(permission_mode)
        self.headless = headless
        self.on_event = on_event
        self.on_text_delta = on_text_delta
        self.cwd = cwd or Path.cwd()
        self.tools = build_registry()

    async def run(
        self,
        user_prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run the agent loop, yielding stream events."""
        messages: list[Message] = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=user_prompt),
        ]

        for turn in range(self.max_turns):
            logger.info("Turn %d/%d", turn + 1, self.max_turns)
            stream = self.router.stream(
                messages, self.tools.specs(), model=model, max_tokens=max_tokens, temperature=temperature
            )

            tool_calls: list[ToolCall] = []
            text_parts: list[str] = []
            final_message: Message | None = None

            async for event in stream:
                if event.type == "text_delta" and event.text:
                    text_parts.append(event.text)
                    if self.on_text_delta:
                        await self.on_text_delta(event.text)
                elif event.type == "tool_call" and event.tool_call:
                    tool_calls.append(event.tool_call)
                elif event.type == "done" and event.message:
                    final_message = event.message
                yield event

            if final_message:
                messages.append(final_message)
                # If no tool calls, we're done
                if not final_message.tool_calls:
                    logger.info("Agent finished (no tool calls)")
                    return

            # Execute tool calls sequentially and yield results
            for tc in tool_calls:
                result = await self._execute_tool(tc)
                tool_msg = Message(
                    role="tool",
                    content=result.get("content") if "content" in result else str(result),
                    tool_call_id=tc.id,
                    name=tc.name,
                )
                messages.append(tool_msg)
                # Yield the tool result as a stream event
                yield StreamEvent(type="done", message=tool_msg)

        logger.warning("Max turns (%d) reached", self.max_turns)

    async def _execute_tool(self, tool_call: ToolCall) -> dict[str, Any]:
        """Execute a single tool call with permission checking."""
        spec, handler = self.tools.get(tool_call.name) or (None, None)
        if not handler:
            return {"error": f"Unknown tool: {tool_call.name}"}

        # Permission check
        if spec.side_effect:
            allowed, message = check_permission(self.permission_mode, tool_call.name, headless=self.headless)
            if not allowed:
                return {"error": message}

        args = tool_call.arguments
        try:
            return await handler(args, cwd=self.cwd)
        except Exception as e:
            logger.exception("Tool %s failed", tool_call.name)
            return {"error": f"Tool execution failed: {e}"}
