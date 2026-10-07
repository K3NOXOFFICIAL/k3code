"""``/advisor``: a strong-tier critical review of the conversation, kept out of the main context."""

from __future__ import annotations

import json
import re
from typing import Any

from k3code.providers.types import Message
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
SUMMARY_SYSTEM = "Summarize this coding conversation: the goal, decisions, work done, open problems. Max 300 words."


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
            session_id=session_id, max_tokens=600, timeout=45,
        )
        return f"[summary of earlier conversation]\n{res.text}\n\n[recent messages]\n{tail}"
    except Exception:  # noqa: BLE001 - fall back to a hard truncation
        return text[-threshold:]


async def advise(
    caller: Any, context: str, question: str = "", *, session_id: str = "", brief: bool = False, timeout: float = 90
) -> str:
    """Ask the strong tier for a critique; the result is returned, never added to the context here."""
    ask = question.strip() or "Review the approach so far: risks, blind spots, and what to do next."
    res = await caller.complete(
        TaskKind.ADVISOR,
        [
            Message(role="system", content=BRIEF_SYSTEM if brief else ADVISOR_SYSTEM),
            Message(role="user", content=f"Conversation:\n{context}\n\nQuestion: {ask}"),
        ],
        session_id=session_id, max_tokens=900, timeout=timeout,
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
            session_id=session_id, max_tokens=500, timeout=60,
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
