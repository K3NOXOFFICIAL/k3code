"""Run one eval task against the real AgentLoop with a scripted model.

The scripted provider holds ordered rules. On each call it joins the text of the messages after the model's last
reply (the prompt on the first call, then tool results, harness notes and a new user message) and answers with the
first rule whose ``match`` appears in it; an empty ``match`` always wins. ``$CWD``, ``$HOME``, ``$ROOT`` and ``$PY``
in call arguments are replaced at run time. Steps are provider calls.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.agent.loop import AgentLoop
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage
from k3code.router import Router, build_chain
from k3code.tools import jobs


@dataclass(frozen=True)
class Call:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Rule:
    """``reply`` is the text answer, a tuple of calls, or a callable from the seen text to either."""

    match: str
    reply: str | tuple[Call, ...] | Callable[[str], str | tuple[Call, ...]]


@dataclass
class Ctx:
    """What a task's hook and check see: the dirs, the loop, and what each model call saw."""

    root: Path
    cwd: Path
    home: Path
    loop: AgentLoop | None = None
    seen: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def transcript(self) -> str:
        return "\n".join(self.seen)


@dataclass(frozen=True)
class Result:
    passed: bool
    steps: int
    detail: str


def _subst(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, real in env.items():
            value = value.replace(key, real)
    return value


def _window(messages: list[Message]) -> str:
    """The text the model has not answered yet: after its last reply, or from the first user message."""
    last = max((i for i, m in enumerate(messages) if m.role == "assistant"), default=None)
    if last is None:
        last = next((i for i, m in enumerate(messages) if m.role == "user"), len(messages)) - 1
    return "\n".join(str(m.content or "") for m in messages[last + 1 :])


class ScriptedProvider:
    name = "fake"
    base_url = "https://fake.test"

    def __init__(self, rules: tuple[Rule, ...], ctx: Ctx, env: dict[str, str]):
        self._rules, self._ctx, self._env = rules, ctx, env
        self.calls = 0

    def _answer(self, seen: str) -> str | tuple[Call, ...]:
        for rule in self._rules:
            if rule.match in seen:
                return rule.reply(seen) if callable(rule.reply) else rule.reply
        return "no rule matched"

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        self.calls += 1
        seen = _window(messages)
        self._ctx.seen.append(seen)
        reply = self._answer(seen)
        if isinstance(reply, str):
            yield StreamEvent(type="text_delta", text=reply)
            yield StreamEvent(type="done", message=Message(role="assistant", content=reply), usage=Usage())
            return
        calls = [
            ToolCall(
                id=f"c{self.calls}_{n}",
                name=c.name,
                arguments={k: _subst(v, self._env) for k, v in c.args.items()},
            )
            for n, c in enumerate(reply)
        ]
        for tc in calls:
            yield StreamEvent(type="tool_call", tool_call=tc)
        yield StreamEvent(type="done", message=Message(role="assistant", content=None, tool_calls=calls), usage=Usage())

    async def aclose(self):
        pass


async def _drive(task: Any, loop: AgentLoop, ctx: Ctx) -> None:
    """One run of the prompt; with ``task.resume``, interrupt at the first tool result and run the resume prompt."""
    async for event in loop.run(task.prompt):
        if task.resume and event.type == "done" and event.message is not None and event.message.role == "tool":
            loop.interrupt()
    if task.resume:
        ctx.notes["after_first_run"] = sorted(p.name for p in ctx.cwd.iterdir())
        loop.reset_interrupt()
        history = [m for m in (loop.turn_messages or []) if m.role != "system"]
        async for _ in loop.run(task.resume, history=history):
            pass


async def run_task(task: Any, root: Path) -> Result:
    """Run ``task`` in a fresh ``root/work`` dir and score it with its check."""
    cwd = root / "work"
    cwd.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(root=root, cwd=cwd, home=Path.home())
    env = {"$CWD": str(cwd), "$HOME": str(ctx.home), "$ROOT": str(root), "$PY": sys.executable}
    for rel, text in task.files.items():
        (cwd / rel).write_text(text)
    provider = ScriptedProvider(task.rules, ctx, env)
    try:
        loop = AgentLoop(
            Router(build_chain([provider], [["fake-model"]]), max_retries=0),
            system_prompt="t",
            max_turns=task.max_turns,
            permission_mode=task.mode,
            cwd=cwd,
            session=task.id,
        )
        loop._sandbox_argv = lambda **_: None  # these evals score the loop, not bwrap
        ctx.loop = loop
        if task.hook:
            task.hook(ctx)
        await _drive(task, loop, ctx)
        failure = task.check(ctx)
    except Exception as e:  # a task that crashes the harness fails; the suite still scores the others
        failure = f"raised {e!r}"
    finally:
        await jobs.REGISTRY.reap(task.id)
    return Result(passed=failure is None, steps=provider.calls, detail=failure or "ok")
