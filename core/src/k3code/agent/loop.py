"""Agent loop: system prompt + messages → router → tool calls → repeat."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from k3code import context_budget
from k3code.paths import home as k3code_home
from k3code.permissions import EXIT_PLAN_TOOL, Decision, PermissionMode
from k3code.permissions.state import PermissionState
from k3code.providers.types import INVALID_TOOL_CALL, Message, StreamEvent, ToolCall
from k3code.reliability import Reliability, ReliabilitySettings, sandbox
from k3code.reliability.loopguard import Verdict
from k3code.router import Router, RouterEvent
from k3code.toolerrors import Failure, describe_call, failure_of
from k3code.tools import (
    MAX_TOOL_RESULT_CHARS,
    SESSION_TOOLS,
    build_registry,
    clip_tool_results,
    format_tool_result,
)
from k3code.tools.validate import invalid_arguments
from k3code.userhooks import HookOutcome, HookRunner

CUT_OFF_CONTINUE = (
    "Your previous answer was cut off at the output token limit. Continue exactly where it stopped; "
    "do not repeat what you already wrote."
)
CUT_OFF_CALL = (
    "Your reply hit the output token limit before this call's arguments were complete, so it did not run. "
    "Re-send it; split large content (a long file, a big edit) into several smaller calls."
)

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
        max_turns: int = 0,
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
        context_window: int | None = None,
    ) -> None:
        self.router = router
        #: the session this loop runs for: background bash jobs are filed under it
        self.session_id = session
        #: Tokens the model takes; past ELIDE_AT_RATIO of it, old tool results are elided from requests (None = never)
        self.context_window = context_window
        #: Per run(): tool results elided from requests so far (they stay elided), results an "unchanged" read points
        #: at (never elided), and successful reads by (path, mtime, size, range) → (step, tool call id).
        self._elided: set[str] = set()
        self._pinned: set[str] = set()
        self._reads: dict[tuple[Any, ...], tuple[int, str]] = {}
        self._step = 0
        #: M1: how much of one tool result the model is sent (the transcript keeps all of it); None = the default
        self.tool_output_chars = tool_output_chars
        #: M4a: what this loop is for (routes to a tier; tagged on usage rows).
        self.task_kind = task_kind
        #: Stop the loop once this many tool calls in a row failed (0 = never); see escalation_reason.
        self.max_tool_errors = max_tool_errors
        self._tool_errors = 0
        self._truncated: set[str] = set()  # ids of tool calls cut off by the output limit: never executed
        self._continuations = 0  # "continue" nudges after a text answer cut off by the output limit
        #: the consecutive failed calls behind _tool_errors: (call, first error line), listed when the turn stops
        self._failed: list[tuple[str, str]] = []
        #: end a tool-error stop with an assistant message listing the failures (off when a higher tier continues)
        self.tool_error_stop_message = True
        #: called after every tool call with (call, result, failure or None); the gateway's learning hub records
        #: tool errors with it. A string it returns is a lesson for the failure, sent after the step's tool results.
        self.on_tool_outcome: Callable[[ToolCall, dict[str, Any], Failure | None], str | None] | None = None
        #: (tool, signature) pairs whose lesson was already sent this run: one reminder per signature per turn
        self._learned: set[tuple[str, str]] = set()
        #: Set when the loop stopped before the task was done: "tool_errors" | "loop_guard" (the attempt looks stuck)
        #: or "max_turns" (the configured cap on model calls was reached).
        self.escalation_reason: str | None = None
        self.system_prompt = system_prompt
        #: Model calls one run() may make; 0 (the default) = no cap: a task runs until the model answers without a
        #: tool call. A cap that is reached ends the run with a message saying so (it used to end silently).
        self.max_turns = max_turns
        #: end a max_turns stop with an assistant message (off for the planning loop, whose last text is the plan)
        self.max_turns_stop_message = True
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
        #: the user's PreToolUse/PostToolUse hooks (k3code.userhooks); None = none
        self.hooks: HookRunner | None = None
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
        """Request cancellation of the running turn (checked between steps and between streamed events)."""
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
        self._elided, self._pinned, self._reads, self._step, self._learned = set(), set(), {}, 0, set()
        # an earlier run's failed calls must not count toward (or be listed in) this run's tool-error stop
        self._tool_errors, self._failed, self._truncated, self._continuations = 0, [], set(), 0
        self.escalation_reason = None  # the REPL reuses one loop: a stop in an earlier run is not this run's
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
                        content=format_tool_result(result),
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                    messages.append(tool_msg)
                    yield StreamEvent(type="done", message=tool_msg)
                if not open_calls and last.role != "tool":
                    messages.append(Message(role="user", content=user_prompt))
                self.turn_messages = messages  # the resumed transcript replaced the list
        self.reliability.save_transcript(messages)

        for turn in itertools.count():
            if 0 < self.max_turns <= turn:
                break
            if self.interrupted:
                logger.info("Turn %d interrupted before start", turn + 1)
                return
            logger.info("Turn %d%s", turn + 1, f"/{self.max_turns}" if self.max_turns > 0 else "")
            self._drain_steer(messages)
            # M2: disk guard + budget check before starting new work.
            self._check_disk_guard()
            self._check_budgets("turn start")
            # M2: the stream goes through persistent retry (pause/park/resume). The model gets head+tail clips of
            # long tool results; ``messages`` (transcript, session, gateway) keeps every full result.
            specs = self.tool_specs()
            stream = self.reliability.stream(
                self.router,
                self._request_messages(messages, specs),
                specs,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )

            tool_calls: list[ToolCall] = []
            final_message: Message | None = None
            text_parts: list[str] = []

            async for event in stream:
                if self.interrupted and final_message is None:
                    break  # /stop while the model is still answering: stop reading (and paying for) the rest
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

            if final_message is None and self.interrupted:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()  # closes the HTTP response: the provider stops generating
                    except Exception:
                        logger.debug("closing the interrupted stream failed", exc_info=True)
            if final_message:
                messages.append(final_message)
                self.reliability.save_transcript(messages)
                cut = final_message.stop_reason == "max_tokens"
                # a call cut off mid-arguments is answered with an error instead of running half-parsed input
                self._truncated = (
                    {c.id for c in final_message.tool_calls if "_unparsed" in c.arguments} if cut else set()
                )
                if final_message.tool_calls and self.on_checkpoint:
                    self.on_checkpoint()  # the tool call is about to run (maybe for an hour): persist what exists
                # The final message's tool_calls is the authoritative list (see note
                # above); prefer it over whatever was accumulated from live events.
                if final_message.tool_calls:
                    tool_calls = final_message.tool_calls
                else:
                    if cut and self._continuations < 3:
                        # the answer hit the output limit: it is not finished, so ask for the rest
                        self._continuations += 1
                        logger.info("Answer cut off at max_tokens: asking the model to continue")
                        messages.append(Message(role="user", content=CUT_OFF_CONTINUE))
                        continue
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

            # Execute the tool calls and yield their results in the model's order. Consecutive read-only calls run
            # together (see _batches); every other call runs alone.
            for batch in self._batches(tool_calls):
                if self.interrupted:
                    logger.info("Interrupted before tool %s", batch[0].name)
                    self.turn_messages = messages
                    return
                steps = []
                for _ in batch:  # in call order, before dispatch: the "unchanged since the read at step N" pointers
                    self._step += 1
                    steps.append(self._step)
                if len(batch) == 1:
                    results = [await self._execute_tool(batch[0])]
                else:
                    results = await asyncio.gather(
                        *(self._execute_tool(tc, step=n) for tc, n in zip(batch, steps, strict=True))
                    )
                for tc, result in zip(batch, results, strict=True):
                    tool_msg = Message(
                        role="tool",
                        content=format_tool_result(result),
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                    messages.append(tool_msg)
                    self.reliability.save_transcript(messages)
                    # Yield the tool result as a stream event
                    yield StreamEvent(type="done", message=tool_msg)
                    failure = self._observe_result(tc, result, notes)
                    if "error" in result and "content" not in result:
                        self._tool_errors += 1
                        self._failed.append(
                            (describe_call(tc.name, tc.arguments), failure.first_line if failure else "")
                        )
                    else:
                        self._tool_errors, self._failed = 0, []
                    if self.max_tool_errors and self._tool_errors >= self.max_tool_errors:
                        self.escalation_reason = "tool_errors"
                        if self.tool_error_stop_message:
                            for stop_event in self._stop_for_tool_errors(messages):
                                yield stop_event
                        self.turn_messages = messages
                        return

            messages.extend(notes)
            self.turn_messages = messages

        # Only a configured cap gets here. Ending as if the task were finished hid it: the gateway reported the turn
        # done and an active goal judged a half-done reply. Say so and let the gateway end the turn as needs_input.
        logger.warning("Max turns (%d) reached", self.max_turns)
        self.escalation_reason = "max_turns"
        if self.max_turns_stop_message:
            text = (
                f"I stopped after {self.max_turns} model calls: the configured `max_turns` cap ({self.max_turns}) "
                "was reached before the task was finished. Reply to continue where I left off, or raise "
                "`max_turns` in the config (0 = no limit)."
            )
            if self.on_text_delta:
                await self.on_text_delta(text)
            stop_msg = Message(role="assistant", content=text)
            messages.append(stop_msg)
            self.reliability.save_transcript(messages)
            yield StreamEvent(type="done", message=stop_msg)
        self.turn_messages = messages

    # Parallel read-only calls. Safe because a call joins a batch only if its spec says side_effect=False (it changes
    # nothing another call of the batch could observe), it is not `bash` (which can do anything whatever its spec
    # says) or exit_plan (it changes the mode and may prompt), and the permission decision of every call in the batch
    # is a plain allow (no approval prompt can overlap another). Everything else is a barrier that runs alone, in order.
    # Results are appended, journaled and observed in call order after the batch finishes, and each call carries the
    # step number it would have had when run alone.
    def _parallel_ok(self, tc: ToolCall) -> bool:
        if tc.name in ("bash", EXIT_PLAN_TOOL) or tc.name == INVALID_TOOL_CALL:
            return False
        found = self.tools.get(tc.name)
        return found is not None and not found[0].side_effect

    def _batches(self, tool_calls: list[ToolCall]) -> list[list[ToolCall]]:
        """Split the calls of one reply into batches: runs of consecutive parallel-safe calls, and singletons."""
        batches: list[list[ToolCall]] = []
        run: list[ToolCall] = []

        def flush() -> None:
            if len(run) > 1 and all(self._plain_allow(tc) for tc in run):
                batches.append(list(run))
            else:
                batches.extend([tc] for tc in run)
            run.clear()

        for tc in tool_calls:
            if self._parallel_ok(tc):
                run.append(tc)
            else:
                flush()
                batches.append([tc])
        flush()
        return batches

    def _plain_allow(self, tc: ToolCall) -> bool:
        spec = self.tools.get(tc.name)[0]
        if invalid_arguments(tc.name, spec.parameters, tc.arguments) is not None:
            return True  # answered with an error before any permission check
        return self.permissions.decide(tc.name, tc.arguments, headless=self.headless).action == "allow"

    async def _execute_tool(self, tool_call: ToolCall, *, step: int | None = None) -> dict[str, Any]:
        """Execute a single tool call with permission checking."""
        if tool_call.name == INVALID_TOOL_CALL:
            # The provider could not parse the model's <tool_calls> block. Nothing ran, so no permission check, hook
            # or journal intent. The error result counts as a normal failed call (tool-error counter, loop guard),
            # so a model that keeps writing invalid blocks is stopped like one that keeps failing a tool.
            reason = tool_call.arguments.get("error", "unparseable")
            return {
                "error": f"Your <tool_calls> block was not valid JSON ({reason}). "
                "Re-send the calls as a valid JSON array."
            }
        if tool_call.id in self._truncated:
            return {"error": CUT_OFF_CALL}
        spec, handler = self.tools.get(tool_call.name) or (None, None)
        if not handler:
            return {"error": f"Unknown tool: {tool_call.name}"}

        args = tool_call.arguments
        if tool_call.name == EXIT_PLAN_TOOL:
            return await self._exit_plan(args)
        # before the permission prompt: a call the handler cannot run is not worth an approval
        if (invalid := invalid_arguments(tool_call.name, spec.parameters, args)) is not None:
            return {"error": invalid}
        decision = self.permissions.decide(tool_call.name, args, headless=self.headless)
        if decision.action == "deny":  # hardline and deny rules: no hook can turn these into an allow
            return {"error": decision.message or f"Permission denied: {tool_call.name}"}
        pre = await self._run_hooks("PreToolUse", tool_call.name, {"tool_input": args})
        if pre.blocked:
            return {"error": f"Blocked by a PreToolUse hook: {pre.reason}"}
        # a hook's "approve" answers the prompt, but never one only a human may answer
        if decision.action == "ask" and not (pre.approved and not decision.needs_human):
            if self.approval_callback is None:
                return {"error": f"Permission denied: {tool_call.name} requires approval (no prompter)"}
            answer = await self.approval_callback(tool_call.name, args, decision)
            if not answer.allowed:
                what = _preview(tool_call.name, args)
                return {"error": f"User denied: {what}" + (f" — {answer.reason}" if answer.reason else "")}
        elif decision.auto_allowed and self.on_auto_allow is not None:
            self.on_auto_allow(tool_call.name, args, decision)

        read_key = self._read_key(args) if tool_call.name == "read" else None
        if read_key is not None and (seen := self._reads.get(read_key)) and seen[1] not in self._elided:
            step, call_id = seen
            self._pinned.add(call_id)  # the earlier result must stay in the requests: this one points at it
            return {
                "content": f"[unchanged since the read at step {step} (call {call_id}): same path, size and "
                "modification time; that result above still holds]"
            }
        # M2: fsync a journal intent before the tool runs.
        self.reliability.journal_intent(tool_call, side_effect=spec.side_effect)
        try:
            if tool_call.name == "bash":
                # the bwrap probe runs a subprocess (up to 10 s): never on the event loop that serves every session
                argv = await asyncio.to_thread(self._sandbox_argv)
                result = await handler(args, cwd=self.cwd, sandbox=argv, session_id=self.session_id)
            elif tool_call.name in SESSION_TOOLS:  # background jobs belong to the session that started them
                result = await handler(args, cwd=self.cwd, session_id=self.session_id)
            else:
                result = await handler(args, cwd=self.cwd)
        except sandbox.SandboxRefused as exc:  # raised before the handler: nothing was spawned
            result = {"error": f"bash refused: {exc}"}
        except Exception as e:
            logger.exception("Tool %s failed", tool_call.name)
            result = {"error": f"Tool execution failed: {type(e).__name__}: {e}"}
        # M2: completion digest, so resume knows this call finished.
        self.reliability.journal_done(tool_call.id, result)
        if read_key is not None and "first" in result:
            self._reads[read_key] = (self._step if step is None else step, tool_call.id)
        post = await self._run_hooks(
            "PostToolUse", tool_call.name, {"tool_input": args, "tool_response": format_tool_result(result)}
        )
        feedback = "\n".join(t for t in (post.reason if post.blocked else "", post.context_text()) if t)
        if feedback:  # the tool already ran: the hook's word joins its result for the model
            result = {**result, "content": f"{format_tool_result(result)}\n\n[PostToolUse hook] {feedback}"}
        return result

    async def _run_hooks(self, event: str, tool: str, payload: dict[str, Any]) -> HookOutcome:
        if not self.hooks:
            return HookOutcome()
        return await self.hooks.run(event, {"tool_name": tool, **payload}, tool_name=tool)

    def _read_key(self, args: dict[str, Any]) -> tuple[Any, ...] | None:
        """What makes two reads the same: the file (path, mtime, size) and the requested range; None if no file."""
        from k3code.tools import _resolve_path

        try:
            path = _resolve_path(str(args.get("path", "")), self.cwd)
            st = path.stat()
        except (OSError, ValueError):
            return None
        window = tuple(args.get(k) for k in ("offset", "limit", "start", "end"))
        return (str(path), st.st_mtime_ns, st.st_size, window)

    def _request_messages(self, messages: list[Message], specs: list[Any]) -> list[Message]:
        """What the provider is sent: long tool results clipped, and once the request passes ELIDE_AT_RATIO of the
        context window, old tool results elided. ``messages`` (transcript, session) keeps every full result."""
        wire = clip_tool_results(messages, self.tool_output_chars or MAX_TOOL_RESULT_CHARS)
        if not self.context_window:
            return wire
        estimate = context_budget.overhead_tokens(self.system_prompt, specs) + context_budget.message_tokens(wire)
        over = estimate > self.context_window * context_budget.ELIDE_AT_RATIO
        if not over and not self._elided:
            return wire
        return context_budget.elide_old_results(wire, over=over, elided=self._elided, keep=self._pinned)

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

    def _observe_result(self, tc: ToolCall, result: dict[str, Any], notes: list[Message]) -> Failure | None:
        """Tell the learning hook and the loop guard how a call went; queue into ``notes`` the hook's lesson for the
        failure (once per signature per run) and the guard's reminder (one per step: the request guard's note, when
        there is one, already says to change approach; a lesson does not count against it)."""
        tag = "[learned] "
        failure = failure_of(tc.name, tc.arguments, result)
        if self.on_tool_outcome is not None:
            try:
                learned = self.on_tool_outcome(tc, result, failure)
            except Exception:  # noqa: BLE001 - learning must never break a turn
                logger.warning("tool outcome hook failed", exc_info=True)
                learned = None
            if learned and failure is not None and (tc.name, failure.signature) not in self._learned:
                self._learned.add((tc.name, failure.signature))
                notes.append(Message(role="system", content=tag + learned))
        outcome = self.reliability.observe_tool_result(
            tc, failure.signature if failure else None, describe_call(tc.name, tc.arguments)
        )
        guarded = any(not (n.content or "").startswith(tag) for n in notes)
        if outcome is not None and outcome.verdict is Verdict.NOTE and outcome.note and not guarded:
            notes.append(Message(role="system", content=outcome.note))
        return failure

    def _stop_for_tool_errors(self, messages: list[Message]) -> Any:
        """Yield a final assistant message listing the failed calls that stopped the turn."""
        counts: dict[tuple[str, str], int] = {}
        for item in self._failed:
            counts[item] = counts.get(item, 0) + 1
        lines = [
            f"- {call}: {error or 'failed'}" + (f" (×{n})" if n > 1 else "") for (call, error), n in counts.items()
        ]
        stop_msg = Message(
            role="assistant",
            content=f"I stopped because {self._tool_errors} tool calls in a row failed:\n"
            + "\n".join(lines)
            + "\nPlease tell me how to proceed (or fix what they need) and I will continue.",
        )
        messages.append(stop_msg)
        yield StreamEvent(type="done", message=stop_msg)

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
