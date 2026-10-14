"""``/advisor``: a stronger model the session's main model consults, kept out of the main context.

Two paths share one call: the session advisor (``/advisor <model-key>``) gives the main model an ``advisor`` tool
for plans, reviews and when it is stuck (Claude Code's advisor mode); ``/advisor ask`` is a one-shot critique."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from k3code.providers.types import Message, ToolSpec
from k3code.routing.tiers import TaskKind

ADVISOR_SYSTEM = (
    "You are a senior engineer reviewing a coding agent's work with the user. Be critical and concrete: "
    "judge the approach, name the risks, and give the next steps. Keep it under 250 words."
)
BRIEF_SYSTEM = ADVISOR_SYSTEM + " This is a brief critique of a freshly approved plan: at most 5 bullets."
DONE_SYSTEM = (
    "You decide whether a goal is really done. Reply with ONE JSON object: "
    '{"blocking": bool, "issues": [str]}. blocking=true only for issues that mean the goal is NOT met '
    "(failing tests, missing requirement, unverified claim)."
)
TOOL_SYSTEM = (
    "You are the advisor of a coding agent: a stronger model it consults mid-task when it needs a plan, a review, "
    "or is stuck. You see its question and the conversation so far, and you cannot run tools. Answer the question "
    "directly: give a concrete plan or diagnosis, name wrong assumptions and risks, and say what to check next. "
    "Under 400 words."
)
SUMMARY_SYSTEM = (
    "Summarize this coding conversation so the work can continue from the summary alone. Keep, in this order: the "
    "goal and constraints the user gave; decisions made and why; files read, created or changed (exact paths) and "
    "what changed in them; commands run and what they showed (failing tests, errors and their fixes); open problems; "
    "the next step. Name identifiers, paths and error messages exactly; drop pleasantries and anything already "
    "finished and irrelevant. Max 400 words."
)
#: Per tool call in a rendered transcript: the call's arguments are cut to this many characters.
CALL_ARG_CHARS = 200


def summary_system(instructions: str = "") -> str:
    """SUMMARY_SYSTEM, plus the user's own focus for this summary (``/compact <focus>``)."""
    focus = instructions.strip()
    if not focus:
        return SUMMARY_SYSTEM
    return f"{SUMMARY_SYSTEM} The user asks the summary to focus on: {focus[:500]}"


def _call_line(call: Any) -> str:
    name, args = (call.get("name"), call.get("arguments")) if isinstance(call, dict) else (call.name, call.arguments)
    text = json.dumps(args, ensure_ascii=False) if args else ""
    return f"{name}({text[:CALL_ARG_CHARS]}{'…' if len(text) > CALL_ARG_CHARS else ''})"


def _clip_middle(text: str, limit: int) -> str:
    """``text`` cut to about ``limit`` characters keeping its start and its end (the end of a command's output is where
    the result and the error are)."""
    if len(text) <= limit:
        return text
    head = limit // 2
    return f"{text[:head]}…{text[-(limit - head) :]}"


#: ``/advisor on`` without a model key: the strong tier of the configured routing.
STRONG = "strong"
#: Session meta key; present (even empty) means the session chose, absent means the config default applies.
META_KEY = "advisor"

TOOL_SPEC = ToolSpec(
    name="advisor",
    description=(
        "Consult your advisor, a stronger model, with a question. It sees the conversation so far. Use it before "
        "committing to a large or risky plan, when you are stuck after a couple of failed attempts, or to review a "
        "risky change before you finish. Calls are slow and costly: ask sparingly, with one precise question."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "What you need: a plan, a diagnosis, a review, a decision."},
            "context": {
                "type": "string",
                "description": "Optional facts the advisor cannot see in the conversation (file excerpts, errors).",
            },
        },
        "required": ["question"],
    },
    side_effect=False,
)
PROMPT_NOTE = (
    "You have an advisor: a stronger model reachable through the `advisor` tool. Consult it before a large or risky "
    "plan, when you are stuck, and before finishing a risky change; otherwise work on your own. It cannot run tools, "
    "so give it the facts it needs."
)


def session_advisor(meta: dict[str, Any] | None, default: str = "") -> str:
    """The session's advisor: a model key, :data:`STRONG`, or "" when off. The session's choice beats the default."""
    if meta and META_KEY in meta:
        return str(meta.get(META_KEY) or "")
    return str(default or "")


def transcript_text(
    messages: list[Message] | list[dict[str, Any]],
    *,
    per_message: int = 1500,
    per_role: Mapping[str, int] | None = None,
    whole_prefix: str = "",
) -> str:
    """The conversation as text for a side model: who said what, with the tool calls the assistant made (name and
    cut-off arguments) and the name of the tool each result came from. A call has no text of its own, so a transcript
    of ``content`` alone showed a summarizer file contents without the edits and commands that led to them.

    ``per_message`` is how much of one message is shown; ``per_role`` overrides it for a role, and a tool result is cut
    keeping its start and its end. A message that starts with ``whole_prefix`` (an earlier summary) is shown whole."""
    from k3code.session_ai import is_shell_record

    lines: list[str] = []
    for m in messages:
        get = m.get if isinstance(m, dict) else lambda k, _m=m: getattr(_m, k, None)
        role = get("role")
        content = get("content") or ""
        if role == "system":
            continue
        if content:
            label = f"tool {get('name')}" if role == "tool" and get("name") else role
            text = str(content)
            limit = (per_role or {}).get(role, per_message)
            if whole_prefix and text.startswith(whole_prefix):
                shown = text
            elif is_shell_record(m):  # a `!cmd` the user ran: command output, cut as a tool result is
                label = "user ran (shell)"
                shown = _clip_middle(text, (per_role or {}).get("tool", per_message))
            elif role == "tool" and per_role and role in per_role:
                shown = _clip_middle(text, limit)
            else:
                shown = text[:limit]
            lines.append(f"{label}: {shown}")
        if calls := get("tool_calls") or []:
            lines.append(f"assistant called: {'; '.join(_call_line(c) for c in calls)}")
    return "\n".join(lines)


def _signature(messages: list[Any]) -> str:
    """A fingerprint of a run of messages (what they say, not which objects): to tell that the start of a conversation
    is still the one an earlier summary covered."""
    import hashlib

    digest = hashlib.sha1(usedforsecurity=False)
    for m in messages:
        get = m.get if isinstance(m, dict) else lambda k, _m=m: getattr(_m, k, None)
        digest.update(f"{get('role')}\x00{get('content') or ''}\x00{len(get('tool_calls') or [])}\x01".encode())
    return digest.hexdigest()


async def condensed_context(
    caller: Any, messages: list[Any], *, threshold: int, session_id: str = "", memo: dict[str, Any] | None = None
) -> str:
    """The transcript; a cheap-tier summary of the old part (plus the recent tail) when it is large.

    ``memo`` (kept by the caller between calls on a growing conversation) holds the last summary and the messages it
    covered: when those are still the start of the conversation, only what came after them is summarized, with the
    earlier summary as input, instead of the whole old part again on every call."""
    text = transcript_text(messages)
    if len(text) <= threshold:
        return text
    tail = transcript_text(messages[-4:])
    old = messages[:-4]
    covered = int(memo.get("n", 0)) if memo else 0
    if memo and 0 < covered <= len(old) and memo.get("sig") == _signature(old[:covered]):
        head = f"[summary so far]\n{memo['summary']}\n\n[messages since]\n{transcript_text(old[covered:])}"[
            -threshold * 2 :
        ]
    else:
        head = transcript_text(old)[-threshold * 2 :]
    try:
        res = await caller.complete(
            TaskKind.COMPACTION,
            [Message(role="system", content=SUMMARY_SYSTEM), Message(role="user", content=head)],
            session_id=session_id,
            max_tokens=600,
            timeout=45,
        )
        # e.g. a reasoning model that spent its budget thinking: a memo of "" would hide the part it covers from every
        # later call
        if not res.text.strip():
            raise ValueError("the summary model returned no text")
        if memo is not None:
            memo.update(n=len(old), sig=_signature(old), summary=res.text)
        return f"[summary of earlier conversation]\n{res.text}\n\n[recent messages]\n{tail}"
    except Exception:  # noqa: BLE001 - fall back to a hard truncation
        return text[-threshold:]


async def advise(
    caller: Any,
    context: str,
    question: str = "",
    *,
    session_id: str = "",
    brief: bool = False,
    timeout: float = 90,
    system: str = "",
    router: Any = None,
    max_tokens: int = 900,
) -> str:
    """Ask the advisor (the strong tier, or ``router``'s model); the result is returned, never added to the context
    here."""
    ask = question.strip() or "Review the approach so far: risks, blind spots, and what to do next."
    extra = {"router": router} if router is not None else {}
    res = await caller.complete(
        TaskKind.ADVISOR,
        [
            Message(role="system", content=system or (BRIEF_SYSTEM if brief else ADVISOR_SYSTEM)),
            Message(role="user", content=f"Conversation:\n{context}\n\nQuestion: {ask}"),
        ],
        session_id=session_id,
        max_tokens=max_tokens,
        timeout=timeout,
        **extra,
    )
    return res.text.strip()


async def review_done(caller: Any, goal: str, context: str, *, session_id: str = "") -> tuple[bool, list[str]]:
    """Before ``/goal`` declares done: (blocking, issues). Advisor failure never blocks."""
    try:
        res = await caller.complete(
            TaskKind.ADVISOR,
            [
                Message(role="system", content=DONE_SYSTEM),
                Message(role="user", content=f"Goal: {goal}\n\nConversation:\n{context}"),
            ],
            session_id=session_id,
            max_tokens=500,
            timeout=60,
        )
    except Exception:  # noqa: BLE001
        return False, []
    m = re.search(r"\{.*\}", res.text, re.DOTALL)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    issues = [str(i) for i in (data.get("issues") or [])] if isinstance(data, dict) else []
    return bool(isinstance(data, dict) and data.get("blocking")), issues
