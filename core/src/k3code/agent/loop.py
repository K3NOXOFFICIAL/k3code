"""Agent loop: system prompt + messages → router → tool calls → repeat."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from k3code.config import K3CODE_HOME
from k3code.permissions import PermissionMode, check_permission
from k3code.providers.types import Message, StreamEvent, ToolCall
from k3code.reliability import Reliability, ReliabilitySettings
from k3code.reliability.loopguard import Verdict
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
        reliability: Reliability | ReliabilitySettings | None = None,
        session: str = "default",
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
        # M2: reliability stack; on by default, off via ReliabilitySettings(enabled=False).
        # Journal writes to $K3CODE_HOME unless the passed bundle set another home.
        if reliability is None:
            reliability = Reliability.from_settings(None, session=session, home=K3CODE_HOME)
        elif isinstance(reliability, ReliabilitySettings):
            reliability = Reliability.from_settings(reliability, session=session, home=K3CODE_HOME)
        self.reliability = reliability
        self.reliability.attach_router(router)
        self.reliability.events.add(self._forward_reliability_event)

    def _forward_reliability_event(self, event: Any) -> None:
        """M2: forward reliability.* / net.state events through the loop's on_event."""
        if self.on_event is not None:
            try:
                self.on_event(event)  # type: ignore[arg-type]
            except Exception:
                logger.debug("reliability event sink failed")

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
            # M2: disk guard + budget check before starting new work.
            self._check_disk_guard()
            self._check_budgets("turn start")
            # M2: the stream goes through persistent retry (pause/park/resume).
            stream = self.reliability.stream(
                self.router, messages, self.tools.specs(), model=model, max_tokens=max_tokens, temperature=temperature
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
                    # Some providers may emit per-call events incrementally as they're
                    # parsed off the stream; both real providers (openai_compat,
                    # anthropic) currently don't — they only attach the fully-parsed
                    # list to the final "done" message below, which is the
                    # authoritative source. Accumulate here too in case a future/
                    # other provider relies on this instead.
                    tool_calls.append(event.tool_call)
                elif event.type == "done" and event.message:
                    final_message = event.message
                    # M2: usage accounting + budget check from every completion.
                    self._record_usage(event)
                    self._check_budgets("after model completion")
                yield event

            if final_message:
                messages.append(final_message)
                # The final message's tool_calls is the authoritative list (see note
                # above); prefer it over whatever was accumulated from live events.
                if final_message.tool_calls:
                    tool_calls = final_message.tool_calls
                else:
                    logger.info("Agent finished (no tool calls)")
                    # M2: loop guard on repeated assistant messages.
                    if self._guard_assistant("".join(text_parts), messages):
                        for stop_event in self._stop_for_input(messages):
                            yield stop_event
                    return

            # M2: loop guard on tool calls (corrective note once, then stop).
            stop = await self._guard_tool_calls(tool_calls, messages)
            if stop:
                for stop_event in self._stop_for_input(messages):
                    yield stop_event
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

        # M2: fsync a journal intent before the tool runs.
        self.reliability.journal_intent(tool_call, side_effect=spec.side_effect)
        args = tool_call.arguments
        try:
            result = await handler(args, cwd=self.cwd)
        except Exception as e:
            logger.exception("Tool %s failed", tool_call.name)
            result = {"error": f"Tool execution failed: {e}"}
        # M2: completion digest, so resume knows this call finished.
        self.reliability.journal_done(tool_call.id, result)
        return result

    # ── M2 reliability helpers ──

    def _check_budgets(self, where: str) -> None:
        err = self.reliability.check_budgets()
        if err is not None:
            raise err

    def _check_disk_guard(self) -> None:
        err = self.reliability.check_disk()
        if err is not None:
            raise err

    def _record_usage(self, event: StreamEvent) -> None:
        msg = event.message
        self.reliability.record_usage(msg.usage if msg is not None else None)

    async def _guard_tool_calls(self, tool_calls: list[ToolCall], messages: list[Message]) -> bool:
        """Observe tool calls; inject one corrective note, or True to stop the turn."""
        for tc in tool_calls:
            outcome = self.reliability.observe_tool_request(tc)
            if outcome is None:
                continue
            if outcome.verdict is Verdict.NOTE and outcome.note:
                messages.append(Message(role="system", content=outcome.note))
                return False
            if outcome.verdict is Verdict.STOP:
                return True
        return False

    def _guard_assistant(self, text: str, messages: list[Message]) -> bool:
        """Observe a no-tool assistant message; note once, or True to stop."""
        outcome = self.reliability.observe_assistant(text)
        if outcome is None:
            return False
        if outcome.verdict is Verdict.NOTE and outcome.note:
            messages.append(Message(role="system", content=outcome.note))
            return False
        return outcome.verdict is Verdict.STOP

    def _stop_for_input(self, messages: list[Message]) -> Any:
        """Yield a final assistant message marking the turn stopped (needs_input)."""
        stop_msg = Message(
            role="assistant",
            content="I stopped because I was repeating myself (loop guard). Please give me more input to proceed.",
        )
        messages.append(stop_msg)
        yield StreamEvent(type="done", message=stop_msg)
