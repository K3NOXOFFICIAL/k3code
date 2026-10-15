"""An extractive summary of a compacted span: no model, deterministic, bounded.

What the last tier keeps of the folded messages, in this order: every user prompt (truncated), the assistant's
final answers (their first and last lines), the files that edit/write calls touched, the commands bash ran with
their exit status, the open todo items and any error text. Command lines and error text go through
``k3code.redact.scrub_text`` first: a summary is re-sent to a provider on every later request.
"""

from __future__ import annotations

import re
from typing import Any

from k3code.redact import scrub_text

#: Tool names whose ``path`` argument is a file the turn changed.
EDIT_TOOLS = frozenset({"edit", "write", "patch", "apply_patch", "multi_edit", "notebook_edit"})
#: A bash result's trailing exit line (tools.format_tool_result writes it only for a non-zero exit).
_EXIT_LINE = re.compile(r"^\[exit code (-?\d+)\]$", re.M)
_ERROR_LINE = re.compile(r"^(?:Error:|\[stderr\]|Traceback|[A-Za-z]*Error:)", re.M)

#: Per-item limits. A user prompt is what the task was; it gets the most room.
USER_CHARS = 600
ANSWER_CHARS = 300
COMMAND_CHARS = 160
ERROR_CHARS = 200
MAX_FILES = 40
MAX_COMMANDS = 30
MAX_ERRORS = 10
MAX_TODOS = 30


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _first_and_last_lines(text: str, limit: int) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return _clip(lines[0], limit)
    head, tail = _clip(lines[0], limit // 2), _clip(lines[-1], limit // 2)
    return head if head == tail else f"{head} … {tail}"


def _calls_by_id(messages: list[dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                args = tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {}
                out[str(tc.get("id") or "")] = (str(tc.get("name") or ""), args)
    return out


def _is_final_answer(messages: list[dict[str, Any]], i: int) -> bool:
    """An assistant message with text and no tool calls that is followed by a user turn or ends the span."""
    m = messages[i]
    if m.get("role") != "assistant" or m.get("tool_calls") or not str(m.get("content") or "").strip():
        return False
    return i + 1 >= len(messages) or messages[i + 1].get("role") == "user"


def extractive_summary(messages: list[dict[str, Any]], *, budget_chars: int = 12_000) -> str:
    """The summary text of ``messages`` (the folded span), at most ``budget_chars`` characters.

    Sections are added in order of worth; the one that would overflow the budget is cut, and the rest are left out
    with a note, so the latest prompts are never lost to a long command list.
    """
    calls = _calls_by_id(messages)
    prompts: list[str] = []
    answers: list[str] = []
    files: list[str] = []
    commands: list[str] = []
    errors: list[str] = []
    todos: list[str] = []
    for i, m in enumerate(messages):
        role, content = m.get("role"), str(m.get("content") or "")
        if role == "user" and content.strip():
            prompts.append(_clip(content, USER_CHARS))
        elif role == "assistant":
            if _is_final_answer(messages, i):
                answers.append(_first_and_last_lines(content, ANSWER_CHARS))
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                name, args = str(tc.get("name") or ""), tc.get("arguments") or {}
                if not isinstance(args, dict):
                    continue
                if name in EDIT_TOOLS and args.get("path") and str(args["path"]) not in files:
                    files.append(str(args["path"]))
                elif name == "todo":
                    todos = [
                        _clip(str(t.get("content") or ""), 120)
                        for t in (args.get("todos") or args.get("items") or [])
                        if isinstance(t, dict) and t.get("status") in ("pending", "in_progress")
                    ]
        elif role == "tool":
            name, args = calls.get(str(m.get("tool_call_id") or ""), (str(m.get("name") or ""), {}))
            if name == "bash" and args.get("command"):
                code = _EXIT_LINE.search(content)
                status = f"exit {code.group(1)}" if code else "ok"
                commands.append(f"{status}: {_clip(scrub_text(str(args['command'])), COMMAND_CHARS)}")
            if content.startswith("Error:") or _ERROR_LINE.search(content):
                errors.append(_clip(scrub_text(_error_text(content)), ERROR_CHARS))

    sections: list[tuple[str, list[str]]] = [
        ("User prompts", prompts),
        ("Assistant answers", answers),
        ("Files changed", files[-MAX_FILES:]),
        ("Commands run", commands[-MAX_COMMANDS:]),
        ("Open todos", todos[:MAX_TODOS]),
        ("Errors seen", errors[-MAX_ERRORS:]),
    ]
    lines = ["Extractive summary (no model was reachable): the facts of the folded part, not a narrative."]
    used = len(lines[0]) + 1
    for title, items in sections:
        if not items:
            continue
        block = [f"\n{title}:"]
        used += len(block[0]) + 1
        shown = 0
        for item in items:
            line = f"- {item}"
            if used + len(line) + 1 > budget_chars:
                break
            block.append(line)
            used += len(line) + 1
            shown += 1
        if shown < len(items):
            block.append(f"- …{len(items) - shown} more left out (compaction budget)")
        lines.extend(block)
        if shown < len(items):
            break
    return "\n".join(lines)


def _error_text(content: str) -> str:
    """The line that says what went wrong: a traceback's last line, the first stderr line, else the error line."""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if "Traceback" in content:
        return next((ln for ln in reversed(lines) if not ln.startswith("[exit code")), lines[-1])
    if "[stderr]" in lines:
        after = lines[lines.index("[stderr]") + 1 :]
        return after[0] if after else "[stderr]"
    return next((ln for ln in lines if _ERROR_LINE.match(ln)), lines[0] if lines else "")
