"""``/preview <task>``: a fast, tool-free sketch of the result before any real work."""

from __future__ import annotations

from typing import Any

from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

PREVIEW_SYSTEM = (
    "You sketch what the result of a coding task will look like BEFORE any work is done. You have no tools. "
    "Pick the output by task type:\n"
    "- UI task: an ASCII mockup in a code block.\n"
    "- project scaffolding: a file tree with a one-line purpose per file.\n"
    "- code change: a rough unified diff.\n"
    "Then add exactly 3 bullets under 'Risks:'. Stay short."
)
HINT = "\n\n_Preview only; nothing was changed. Run it for real with /go, or adjust the task._"


async def preview(caller: Any, task: str, *, session_id: str = "", timeout: float = 30) -> str:
    """Sketch ``task`` on the fast tier with no tools and a hard time budget."""
    res = await caller.complete(
        TaskKind.PREVIEW,
        [Message(role="system", content=PREVIEW_SYSTEM), Message(role="user", content=task)],
        session_id=session_id,
        tools=[],
        max_tokens=1200,
        timeout=timeout,
    )
    return res.text.strip() + HINT
