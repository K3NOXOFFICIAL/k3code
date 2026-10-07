"""Agent loop: system prompt + messages → router → tool calls → repeat."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from k3code.permissions import EXIT_PLAN_TOOL, Decision, PermissionMode
from k3code.permissions.state import PermissionState
from k3code.providers.types import Message, StreamEvent, ToolCall
from k3code.router import Router, RouterEvent
from k3code.tools import build_registry

logger = logging.getLogger(__name__)

@dataclass
class ApprovalResult:
    """User's answer to an approval prompt."""

    choice: str = "deny"  # once | session | always | deny
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.choice != "deny"

    def __bool__(self) -> bool:
        return self.allowed


#: Asked when the engine says "ask": (tool, arguments, decision) → answer.
ApprovalCallback = Callable[[str, dict[str, Any], Decision], Awaitable[ApprovalResult]]
#: exit_plan: (plan text) → mode to switch to ("default"/"accept-edits"), or None if rejected.
PlanCallback = Callable[[str], Awaitable[str | None]]
#: Told about every side effect the mode auto-allowed: (tool, arguments, decision).
AutoAllowCallback = Callable[[str, dict[str, Any], Decision], None]


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
        approval_callback: ApprovalCallback | None = None,
        plan_callback: PlanCallback | None = None,
        on_auto_allow: AutoAllowCallback | None = None,
        permissions: PermissionState | None = None,
    ) -> None:
        self.router = router
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.cwd = cwd or Path.cwd()
        self.permissions = permissions or PermissionState(mode=PermissionMode(permission_mode), cwd=self.cwd)
        self.plan_callback = plan_callback
        self.on_auto_allow = on_auto_allow
        self.headless = headless
        self.on_event = on_event
        self.on_text_delta = on_text_delta
        self.approval_callback = approval_callback
        self.tools = build_registry()
        self._interrupt = asyncio.Event()
        #: Conversation messages of the most recent run(), in order (system first).
        #: The gateway persists these after each turn.
        self.turn_messages: list[Message] = []

    @property
    def permission_mode(self) -> PermissionMode:
        return self.permissions.mode

    @permission_mode.setter
    def permission_mode(self, mode: PermissionMode | str) -> None:
        self.permissions.mode = PermissionMode(mode)

    def tool_specs(self) -> list[Any]:
        """Tool specs for the model; ``exit_plan`` is only offered in plan mode."""
        plan = self.permissions.mode == PermissionMode.PLAN
        return [s for s in self.tools.specs() if plan or s.name != EXIT_PLAN_TOOL]

    def interrupt(self) -> None:
        """Request cancellation of the running turn (checked between steps)."""
        self._interrupt.set()

    def reset_interrupt(self) -> None:
        self._interrupt.clear()

    @property
    def interrupted(self) -> bool:
        return self._interrupt.is_set()

    async def run(
        self,
        user_prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 8192,
        temperature: float | None = None,
        history: list[Message] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run the agent loop, yielding stream events.

        ``history`` is prior conversation messages (no system entry); when given,
        the loop continues that conversation instead of starting fresh.
        """
        messages: list[Message] = [
            Message(role="system", content=self.system_prompt),
            *(history or []),
            Message(role="user", content=user_prompt),
        ]
        self.turn_messages = []

        for turn in range(self.max_turns):
            if self.interrupted:
                logger.info("Turn %d interrupted before start", turn + 1)
                return
            logger.info("Turn %d/%d", turn + 1, self.max_turns)
            stream = self.router.stream(
                messages, self.tool_specs(), model=model, max_tokens=max_tokens, temperature=temperature
            )

            tool_calls: list[ToolCall] = []
            final_message: Message | None = None

            async for event in stream:
                if event.type == "text_delta" and event.text:
                    if self.on_text_delta:
                        await self.on_text_delta(event.text)
                elif event.type == "tool_call" and event.tool_call:
                    # Some providers may emit per-call events incrementally as they're
                    # parsed off the stream; both real providers (openai_compat,
                    # anthropic) currently don't — they only attach the fully-parsed
                    # list to the final "done" message below, which is the
                    # authoritative source. Accumulate here too in case a future/
                    # other provider relies on this instead.
                    tool_calls.append(event.tool_call)
                elif event.type == "done" and event.message:
                    final_message = event.message
                yield event

            if final_message:
                messages.append(final_message)
                # The final message's tool_calls is the authoritative list (see note
                # above); prefer it over whatever was accumulated from live events.
                if final_message.tool_calls:
                    tool_calls = final_message.tool_calls
                else:
                    logger.info("Agent finished (no tool calls)")
                    self.turn_messages = messages
                    return
            elif self.interrupted:
                # Stream aborted without a final message (e.g. interrupt during
                # streaming): nothing more to execute.
                self.turn_messages = messages
                return

            # Execute tool calls sequentially and yield results
            for tc in tool_calls:
                if self.interrupted:
                    logger.info("Interrupted before tool %s", tc.name)
                    self.turn_messages = messages
                    return
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

            self.turn_messages = messages

        logger.warning("Max turns (%d) reached", self.max_turns)
        self.turn_messages = messages

    async def _execute_tool(self, tool_call: ToolCall) -> dict[str, Any]:
        """Execute a single tool call with permission checking."""
        spec, handler = self.tools.get(tool_call.name) or (None, None)
        if not handler:
            return {"error": f"Unknown tool: {tool_call.name}"}

        args = tool_call.arguments
        if tool_call.name == EXIT_PLAN_TOOL:
            return await self._exit_plan(args)
        decision = self.permissions.decide(tool_call.name, args, headless=self.headless)
        if decision.action == "deny":
            return {"error": decision.message or f"Permission denied: {tool_call.name}"}
        if decision.action == "ask":
            if self.approval_callback is None:
                return {"error": f"Permission denied: {tool_call.name} requires approval (no prompter)"}
            answer = await self.approval_callback(tool_call.name, args, decision)
            if not answer.allowed:
                what = _preview(tool_call.name, args)
                return {"error": f"User denied: {what}" + (f" — {answer.reason}" if answer.reason else "")}
        elif decision.auto_allowed and self.on_auto_allow is not None:
            self.on_auto_allow(tool_call.name, args, decision)

        try:
            return await handler(args, cwd=self.cwd)
        except Exception as e:
            logger.exception("Tool %s failed", tool_call.name)
            return {"error": f"Tool execution failed: {e}"}

    async def _exit_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.permissions.mode != PermissionMode.PLAN:
            return {"error": "exit_plan is only available in plan mode"}
        plan = str(args.get("plan", "")).strip()
        if not plan:
            return {"error": "exit_plan needs a non-empty plan"}
        if self.plan_callback is None:
            return {"error": "Plan not approved: no interactive approver (headless)"}
        target = await self.plan_callback(plan)
        if target is None:
            return {"error": "User rejected the plan; revise it and call exit_plan again."}
        self.permissions.mode = PermissionMode(target)
        return {"content": f"Plan approved; mode is now {self.permissions.mode.value}. Implement it."}


def _preview(tool: str, args: dict[str, Any]) -> str:
    if tool == "bash":
        return str(args.get("command", ""))[:200]
    return f"{tool} {args.get('path', '')}".strip()
