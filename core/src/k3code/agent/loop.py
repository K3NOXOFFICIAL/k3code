"""Agent loop: system prompt + messages → router → tool calls → repeat."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from k3code.paths import home as k3code_home
from k3code.permissions import EXIT_PLAN_TOOL, Decision, PermissionMode
from k3code.permissions.state import PermissionState
from k3code.providers.types import Message, StreamEvent, ToolCall
from k3code.reliability import Reliability, ReliabilitySettings, sandbox
from k3code.reliability.loopguard import Verdict
from k3code.router import Router, RouterEvent
from k3code.tools import MAX_TOOL_RESULT_CHARS, build_registry, clip_tool_results

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
        reliability: Reliability | ReliabilitySettings | None = None,
        session: str = "default",
        plan_callback: PlanCallback | None = None,
        on_auto_allow: AutoAllowCallback | None = None,
        permissions: PermissionState | None = None,
        background: bool = False,
        unattended: bool = False,
        unattended_network: bool = False,
        task_kind: str = "interactive_turn",
        max_tool_errors: int = 0,
        tool_output_chars: int | None = None,
    ) -> None:
        self.router = router
        #: M1: how much of one tool result the model is sent (the transcript keeps all of it); None = the default
        self.tool_output_chars = tool_output_chars
        #: M4a: what this loop is for (routes to a tier; tagged on usage rows).
        self.task_kind = task_kind
        #: Stop the loop once this many tool calls in a row failed (0 = never); see escalation_reason.
        self.max_tool_errors = max_tool_errors
        self._tool_errors = 0
        #: Set when the loop stopped because the attempt looks stuck: "tool_errors" | "loop_guard".
        self.escalation_reason: str | None = None
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.cwd = cwd or Path.cwd()
        self.permissions = permissions or PermissionState(mode=PermissionMode(permission_mode), cwd=self.cwd)
        self.plan_callback = plan_callback
        self.on_auto_allow = on_auto_allow
        self.headless = headless
        #: Background/cron/loop sessions run bash sandboxed (like auto/yolo mode).
        self.background = background
        #: No human is watching this run (goal continuation, sub-agent): bash is sandboxed whatever the mode.
        self.unattended = unattended
        #: Unattended bash keeps the network only when configured (``autonomy.unattended_network``).
        self.unattended_network = unattended_network
        self._sandbox_warned = False
        self.on_event = on_event
        self.on_text_delta = on_text_delta
        #: called when a retry makes the streamed text so far void (the consumer clears its copy)
        self.on_text_reset: Callable[[], Awaitable[None]] | None = None
        #: called when the assistant's tool call joined the conversation, before the tool runs (the gateway persists)
        self.on_checkpoint: Callable[[], None] | None = None
        #: returns user messages typed mid-turn (session.steer); they join the conversation before the next model call
        self.take_steer: Callable[[], list[str]] | None = None
        self.approval_callback = approval_callback
        self.tools = build_registry()
        self._interrupt = asyncio.Event()
        #: Conversation messages of the most recent run(), in order (system first).
        #: The gateway persists these after each turn.
        self.turn_messages: list[Message] = []
        # M2: reliability stack; on by default, off via ReliabilitySettings(enabled=False).
        # Journal writes to $K3CODE_HOME unless the passed bundle set another home. Read at call time: a module
        # constant would freeze the import-time home (the real ~/.k3code under test imports).
        if reliability is None:
            reliability = Reliability.from_settings(None, session=session, home=k3code_home())
        elif isinstance(reliability, ReliabilitySettings):
            reliability = Reliability.from_settings(reliability, session=session, home=k3code_home())
        self.reliability = reliability
        self.reliability.attach_router(router)
        self.reliability.events.add(self._forward_reliability_event, key="loop")

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
        history: list[Message] | None = None,
        resume: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        """Run the agent loop, yielding stream events.

        ``history`` is prior conversation messages (no system entry); when given,
        the loop continues that conversation instead of starting fresh.
        """
        # The reliability bundle is shared per session; bind its retry wrapper to *this* loop's router
        # (another loop on a different tier may have attached its own since construction).
        self.reliability.attach_router(self.router)
        self.reliability.begin_turn()
        messages: list[Message] = [
            Message(role="system", content=self.system_prompt),
            *(history or []),
            Message(role="user", content=user_prompt),
        ]
        # The same list object the loop keeps appending to: a cancelled or crashed turn can still be persisted
        # (turn_messages used to be assigned only when a step finished, so /stop or SIGTERM lost the whole turn).
        self.turn_messages = messages
        if resume:
            # M2: continue a crashed session from its persisted transcript.
            saved = self.reliability.load_transcript()
            if saved:
                messages = saved
                answered = {m.tool_call_id for m in messages if m.role == "tool"}
                last = messages[-1]
                open_calls = [c for c in last.tool_calls if c.id not in answered] if last.role == "assistant" else []
                for tc in open_calls:
                    # Side-effect tools with an intent but no done are NOT re-run.
                    result = self.reliability.interrupted_for(tc) or await self._execute_tool(tc)
                    tool_msg = Message(
                        role="tool",
                        content=result.get("content") if "content" in result else str(result),
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                    messages.append(tool_msg)
                    yield StreamEvent(type="done", message=tool_msg)
                if not open_calls and last.role != "tool":
                    messages.append(Message(role="user", content=user_prompt))
                self.turn_messages = messages  # the resumed transcript replaced the list
        self.reliability.save_transcript(messages)

        for turn in range(self.max_turns):
            if self.interrupted:
                logger.info("Turn %d interrupted before start", turn + 1)
                return
            logger.info("Turn %d/%d", turn + 1, self.max_turns)
            self._drain_steer(messages)
            # M2: disk guard + budget check before starting new work.
            self._check_disk_guard()
            self._check_budgets("turn start")
            # M2: the stream goes through persistent retry (pause/park/resume). The model gets head+tail clips of
            # long tool results; ``messages`` (transcript, session, gateway) keeps every full result.
            stream = self.reliability.stream(
                self.router,
                clip_tool_results(messages, self.tool_output_chars or MAX_TOOL_RESULT_CHARS),
                self.tool_specs(),
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )

            tool_calls: list[ToolCall] = []
            final_message: Message | None = None
            text_parts: list[str] = []

            async for event in stream:
                if event.type == "text_delta" and event.text:
                    text_parts.append(event.text)
                    if self.on_text_delta:
                        await self.on_text_delta(event.text)
                elif event.type == "reset":
                    # the router is retrying after partial output: forget what this attempt streamed
                    text_parts.clear()
                    tool_calls.clear()
                    if self.on_text_reset:
                        await self.on_text_reset()
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
                self.reliability.save_transcript(messages)
                if final_message.tool_calls and self.on_checkpoint:
                    self.on_checkpoint()  # the tool call is about to run (maybe for an hour): persist what exists
                # The final message's tool_calls is the authoritative list (see note
                # above); prefer it over whatever was accumulated from live events.
                if final_message.tool_calls:
                    tool_calls = final_message.tool_calls
                else:
                    if self._drain_steer(messages):
                        continue  # the user steered while the model answered: answer that too, in this turn
                    logger.info("Agent finished (no tool calls)")
                    self.turn_messages = messages
                    # M2: loop guard on repeated assistant messages.
                    if self._guard_assistant("".join(text_parts), messages):
                        for stop_event in self._stop_for_input(messages):
                            yield stop_event
                    return
            elif self.interrupted:
                # Stream aborted without a final message (e.g. interrupt during
                # streaming): nothing more to execute.
                self.turn_messages = messages
                return

            # M2: loop guard on tool calls (corrective note once, then stop). The note is appended after the tool
            # results: a message between an assistant tool_calls message and its results is a 400 on
            # OpenAI-compatible providers.
            notes: list[Message] = []
            stop = await self._guard_tool_calls(tool_calls, notes)
            if stop:
                for stop_event in self._stop_for_input(messages):
                    yield stop_event
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
                self.reliability.save_transcript(messages)
                # Yield the tool result as a stream event
                yield StreamEvent(type="done", message=tool_msg)
                self._tool_errors = self._tool_errors + 1 if "error" in result and "content" not in result else 0
                if self.max_tool_errors and self._tool_errors >= self.max_tool_errors:
                    self.escalation_reason = "tool_errors"
                    self.turn_messages = messages
                    return

            messages.extend(notes)
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

        # M2: fsync a journal intent before the tool runs.
        self.reliability.journal_intent(tool_call, side_effect=spec.side_effect)
        try:
            if tool_call.name == "bash":
                # the bwrap probe runs a subprocess (up to 10 s): never on the event loop that serves every session
                argv = await asyncio.to_thread(self._sandbox_argv)
                result = await handler(args, cwd=self.cwd, sandbox=argv)
            else:
                result = await handler(args, cwd=self.cwd)
        except sandbox.SandboxRefused as exc:  # raised before the handler: nothing was spawned
            result = {"error": f"bash refused: {exc}"}
        except Exception as e:
            logger.exception("Tool %s failed", tool_call.name)
            result = {"error": f"Tool execution failed: {e}"}
        # M2: completion digest, so resume knows this call finished.
        self.reliability.journal_done(tool_call.id, result)
        return result

    def _sandbox_argv(self) -> list[str] | None:
        """bwrap prefix for bash in sandboxed sessions.

        None = run unsandboxed: interactive auto/yolo sessions when bwrap is unusable (warned once). An unattended
        session (background, goal continuation, sub-agent) has no human to see a warning, so it raises
        :class:`sandbox.SandboxUnavailable` instead: fail closed, nothing runs.
        """
        if not sandbox.should_sandbox(self.permissions.mode, self.background, self.unattended):
            return None
        unattended = self.background or self.unattended
        if not sandbox.usable():
            if unattended:
                raise sandbox.SandboxUnavailable("bubblewrap is unusable here: unattended bash is refused; see /doctor")
            if not self._sandbox_warned:
                self._sandbox_warned = True
                logger.warning("bwrap unavailable: running bash without the sandbox (see /doctor)")
            return None
        network = self.unattended_network if unattended else True
        return sandbox.build_argv(self.cwd, self.permissions.add_dirs, network=network)

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

    def _drain_steer(self, messages: list[Message]) -> bool:
        """Append steering messages (never between an assistant tool_calls message and its results)."""
        texts = self.take_steer() if self.take_steer is not None else []
        for text in texts:
            messages.append(Message(role="user", content=text))
        if texts:
            self.reliability.save_transcript(messages)
        return bool(texts)

    async def _guard_tool_calls(self, tool_calls: list[ToolCall], notes: list[Message]) -> bool:
        """Observe tool calls; queue one corrective note into ``notes``, or True to stop the turn."""
        for tc in tool_calls:
            outcome = self.reliability.observe_tool_request(tc)
            if outcome is None:
                continue
            if outcome.verdict is Verdict.NOTE and outcome.note:
                notes.append(Message(role="system", content=outcome.note))
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
        self.escalation_reason = "loop_guard"
        stop_msg = Message(
            role="assistant",
            content="I stopped because I was repeating myself (loop guard). Please give me more input to proceed.",
        )
        messages.append(stop_msg)
        yield StreamEvent(type="done", message=stop_msg)

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
