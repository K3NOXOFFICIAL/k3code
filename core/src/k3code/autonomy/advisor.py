"""``/advisor``: a stronger model the session's main model consults, kept out of the main context.

Two paths share one call: the session advisor (``/advisor <model-key>``) gives the main model an ``advisor`` tool
for plans, reviews and when it is stuck (Claude Code's advisor mode); ``/advisor ask`` is a one-shot critique."""

from __future__ import annotations

import json
import re
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
SUMMARY_SYSTEM = "Summarize this coding conversation: the goal, decisions, work done, open problems. Max 300 words."


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


def transcript_text(messages: list[Message] | list[dict[str, Any]], *, per_message: int = 1500) -> str:
    lines: list[str] = []
    for m in messages:
        role = m["role"] if isinstance(m, dict) else m.role
        content = (m.get("content") if isinstance(m, dict) else m.content) or ""
        if role == "system" or not content:
            continue
        lines.append(f"{role}: {str(content)[:per_message]}")
    return "\n".join(lines)


async def condensed_context(caller: Any, messages: list[Any], *, threshold: int, session_id: str = "") -> str:
    """The transcript; a cheap-tier summary of the old part (plus the recent tail) when it is large."""
    text = transcript_text(messages)
    if len(text) <= threshold:
        return text
    tail = transcript_text(messages[-4:])
    head = transcript_text(messages[:-4])[-threshold * 2 :]
    try:
        res = await caller.complete(
            TaskKind.COMPACTION,
            [Message(role="system", content=SUMMARY_SYSTEM), Message(role="user", content=head)],
            session_id=session_id,
            max_tokens=600,
            timeout=45,
        )
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
