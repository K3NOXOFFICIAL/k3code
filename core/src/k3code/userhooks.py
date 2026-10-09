"""User hooks: shell commands the user configures to run on agent events, with Claude Code's contract.

Config (``hooks:`` in the user's ``config.yaml``, and in a project's ``.k3code/config.yaml`` once the project is
trusted; both apply, the user's first)::

    hooks:
      PreToolUse:
        - {matcher: "bash|edit", command: "~/bin/check.sh", timeout: 30}
      UserPromptSubmit:
        - {command: "date"}

Claude Code's nested form (``- matcher: X`` with ``hooks: [{type: command, command, timeout}]``) is read too.

The command gets the event as JSON on stdin (``session_id``, ``cwd``, ``hook_event_name``, plus ``tool_name`` and
``tool_input`` for tool events, ``tool_response`` for PostToolUse, ``prompt`` for UserPromptSubmit). Exit 0 is fine;
its stdout may be JSON ``{"decision": "block"|"approve", "reason", "additionalContext"}``. Exit 2 blocks, and stderr
is the reason the model sees. Any other exit status is an error that is logged and blocks nothing. A hook that runs
longer than its timeout (default 60 s) is killed and counts as such an error.

Hooks run as the user, outside the sandbox, with the same scrubbed environment as every child process (no provider
keys, no gateway socket or token variables) plus ``CLAUDE_PROJECT_DIR``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "Stop")
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse"})
#: events whose plain (non-JSON) stdout is added to the model's context, as in Claude Code
CONTEXT_EVENTS = frozenset({"UserPromptSubmit", "SessionStart"})
DEFAULT_TIMEOUT = 60.0
MAX_OUTPUT_CHARS = 10_000
BLOCK_EXIT = 2


@dataclass
class Hook:
    event: str
    command: str
    matcher: str = ""
    timeout: float = DEFAULT_TIMEOUT
    source: str = "user"  # "user" | "project"

    def matches(self, tool_name: str | None) -> bool:
        if self.event not in TOOL_EVENTS or self.matcher in ("", "*"):
            return True
        try:
            return re.fullmatch(self.matcher, tool_name or "", re.IGNORECASE) is not None
        except re.error:
            return self.matcher.lower() == (tool_name or "").lower()


@dataclass
class HookOutcome:
    blocked: bool = False
    approved: bool = False
    reason: str = ""
    context: list[str] = field(default_factory=list)

    def context_text(self) -> str:
        return "\n".join(c for c in self.context if c)


def parse(section: Any, source: str) -> list[Hook]:
    """Hooks from one ``hooks:`` section; malformed entries are skipped with a warning."""
    if not isinstance(section, dict):
        if section:
            logger.warning("%s hooks: expected a mapping of event -> list, ignored", source)
        return []
    hooks: list[Hook] = []
    for event, entries in section.items():
        if event not in EVENTS:
            logger.warning("%s hooks: unknown event %r ignored (known: %s)", source, event, ", ".join(EVENTS))
            continue
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            matcher = str(entry.get("matcher") or "")
            inner = entry.get("hooks") if isinstance(entry.get("hooks"), list) else [entry]  # Claude Code's nesting
            for h in inner:
                if not isinstance(h, dict) or not str(h.get("command") or "").strip():
                    continue
                try:
                    timeout = float(h.get("timeout") or DEFAULT_TIMEOUT)
                except (TypeError, ValueError):
                    timeout = DEFAULT_TIMEOUT
                hooks.append(Hook(str(event), str(h["command"]), matcher, timeout, source))
    return hooks


def load(project_dir: str | Path, session_id: str = "") -> HookRunner:
    """The user's hooks, then the project's when the user trusted the project (k3code.trust)."""
    from k3code import trust
    from k3code.config import load_user_section

    hooks = parse(load_user_section("hooks"), "user")
    text = trust.trusted_text(project_dir)
    if text:
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            data = {}
        if isinstance(data, dict):
            hooks += parse(data.get("hooks"), "project")
    return HookRunner(hooks, session_id=session_id, cwd=Path(project_dir))


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "…"


class HookRunner:
    def __init__(self, hooks: list[Hook], *, session_id: str = "", cwd: str | Path = ".") -> None:
        self.hooks = hooks
        self.session_id = session_id
        self.cwd = Path(cwd)

    def __bool__(self) -> bool:
        return bool(self.hooks)

    def for_event(self, event: str, tool_name: str | None = None) -> list[Hook]:
        return [h for h in self.hooks if h.event == event and h.matches(tool_name)]

    async def run(
        self, event: str, payload: dict[str, Any] | None = None, *, tool_name: str | None = None
    ) -> HookOutcome:
        """Run the event's matching hooks in order; the first block stops the rest. Never raises."""
        outcome = HookOutcome()
        hooks = self.for_event(event, tool_name)
        if not hooks:
            return outcome
        data = {"session_id": self.session_id, "cwd": str(self.cwd), "hook_event_name": event, **(payload or {})}
        stdin = json.dumps(data, default=str).encode("utf-8")
        for hook in hooks:
            result = await self._exec(hook, stdin)
            if result is None:
                continue
            code, out, err = result
            if code == BLOCK_EXIT:
                outcome.blocked, outcome.reason = True, err.strip() or f"blocked by the {event} hook"
                break
            if code != 0:
                logger.warning("%s hook %r exited %s: %s", event, hook.command, code, err.strip()[:300])
                continue
            self._read_stdout(event, out, outcome)
            if outcome.blocked:
                break
        return outcome

    @staticmethod
    def _read_stdout(event: str, out: str, outcome: HookOutcome) -> None:
        text = out.strip()
        parsed: Any = None
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        if not isinstance(parsed, dict):
            if text and event in CONTEXT_EVENTS:
                outcome.context.append(text)
            return
        decision = str(parsed.get("decision") or "").lower()
        reason = str(parsed.get("reason") or "")
        if decision == "block":
            outcome.blocked, outcome.reason = True, reason or f"blocked by the {event} hook"
        elif decision == "approve":
            outcome.approved = True
        specific = parsed.get("hookSpecificOutput")
        extra = parsed.get("additionalContext") or (
            specific.get("additionalContext") if isinstance(specific, dict) else None
        )
        if extra:
            outcome.context.append(str(extra))

    async def _exec(self, hook: Hook, stdin: bytes) -> tuple[int, str, str] | None:
        """(exit code, stdout, stderr), or None when the hook could not run or timed out (logged)."""
        from k3code.reliability.sandbox import child_env
        from k3code.tools import _kill_group

        env = child_env({"CLAUDE_PROJECT_DIR": str(self.cwd)})
        try:
            proc = await asyncio.create_subprocess_shell(
                hook.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd.is_dir() else None,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            logger.warning("%s hook %r could not start: %s", hook.event, hook.command, exc)
            return None
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=hook.timeout)
        except TimeoutError:
            await _kill_group(proc)
            logger.warning("%s hook %r timed out after %ss and was killed", hook.event, hook.command, hook.timeout)
            return None
        except asyncio.CancelledError:
            await _kill_group(proc)
            raise
        return (
            proc.returncode if proc.returncode is not None else -1,
            _clip(out.decode("utf-8", errors="replace")),
            _clip(err.decode("utf-8", errors="replace")),
        )
