"""The decision model: a small, cheap model that picks which old tool results the main model still needs.

Off by default (``context.decision_model.enabled``). Without it, once a request passes the elision threshold every
old tool result longer than ELIDE_MIN_CHARS is replaced by a bare marker (k3code.context_budget). With it, the
decision model reads those results first (shortened) together with the task and the latest step, and answers per
result: keep it whole, or drop it and keep a short note of the facts that still matter (paths, line numbers, values,
errors). The main model then gets fewer input tokens without losing what it is working with.

It runs only where elision runs anyway (a batch, see AgentLoop._request_messages), so it adds no prompt-cache misses:
a result once replaced stays replaced, note included, for the rest of the turn. Any failure (timeout, bad JSON, no
provider) falls back to plain elision.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from k3code.context_budget import ELIDE_AT_RATIO
from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

logger = logging.getLogger(__name__)

SELECT_SYSTEM = (
    "You manage the context of a coding agent. Below are earlier tool results the agent is about to lose from its "
    "context to save tokens. For each one decide: keep it whole only if the agent's next steps still need its exact "
    "text; otherwise it is dropped (the agent can re-run the tool), and you may leave a note of at most 300 "
    "characters with the specific facts still worth knowing (file paths, line numbers, names, values, errors). "
    "Leave no note for results that no longer matter. Keep as little as possible.\n"
    'Reply with JSON only: {"keep": [<numbers>], "notes": {"<number>": "<note>"}}'
)
#: What one candidate shows the decision model: its first this many characters
EXCERPT_CHARS = 1500
#: Characters of a note that reach the main model
NOTE_CHARS = 300
TASK_CHARS = 4000
LATEST_CHARS = 2000


@dataclass(frozen=True)
class DecisionSettings:
    """``context.decision_model`` from config."""

    enabled: bool = False
    #: provider block name to send to ("" = every provider, in chain order); only used with ``model``
    provider: str = ""
    #: model id ("" = the tier ``task_tiers.context_select`` picks, the cheap tier by default)
    model: str = ""
    #: elide (with the decision model) once the request passes this share of the context window
    at_ratio: float = ELIDE_AT_RATIO
    #: characters of tool results one decision reads at most
    max_input_chars: int = 40_000
    timeout: float = 30.0


def decision_settings(config: Any) -> DecisionSettings:
    raw = (getattr(config, "context", None) or {}).get("decision_model")
    if isinstance(raw, bool):
        return DecisionSettings(enabled=raw)
    if not isinstance(raw, dict):
        return DecisionSettings()
    defaults = DecisionSettings()
    return DecisionSettings(
        enabled=bool(raw.get("enabled", False)),
        provider=str(raw.get("provider") or ""),
        model=str(raw.get("model") or ""),
        at_ratio=float(raw.get("at_ratio") or defaults.at_ratio),
        max_input_chars=int(raw.get("max_input_chars") or defaults.max_input_chars),
        timeout=float(raw.get("timeout") or defaults.timeout),
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[{len(text) - limit} more chars]"


def _task(messages: Sequence[Message]) -> str:
    for m in messages:
        if m.role == "user" and m.content:
            return m.content
    return ""


def _latest(messages: Sequence[Message]) -> str:
    for m in reversed(messages):
        if m.role == "assistant":
            calls = "; ".join(f"{tc.name}({json.dumps(tc.arguments, ensure_ascii=False)[:200]})" for tc in m.tool_calls)
            return "\n".join(part for part in (m.content or "", f"calls: {calls}" if calls else "") if part)
    return ""


def build_prompt(messages: Sequence[Message], candidates: Sequence[Message], max_input_chars: int) -> str:
    """What the decision model reads: the task, the latest step and every candidate, numbered from 1."""
    calls = {tc.id: tc for m in messages if m.role == "assistant" for tc in m.tool_calls}
    per = max(200, min(EXCERPT_CHARS, max_input_chars // max(1, len(candidates))))
    parts = [
        "## Task\n" + _clip(_task(messages), TASK_CHARS),
        "## Latest step of the agent\n" + _clip(_latest(messages), LATEST_CHARS),
        "## Earlier tool results",
    ]
    for n, m in enumerate(candidates, 1):
        tc = calls.get(m.tool_call_id or "")
        args = json.dumps(tc.arguments, ensure_ascii=False)[:300] if tc else ""
        content = m.content or ""
        parts.append(f"### [{n}] {m.name or 'tool'} {args} ({len(content)} chars)\n{_clip(content, per)}")
    return "\n\n".join(parts)


def parse_decision(text: str, candidates: Sequence[Message]) -> dict[str, str]:
    """{tool call id: "keep" | note} from the model's JSON; candidates it does not name are dropped without a note."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("no JSON object in the decision")
    data = json.loads(match.group(0))
    ids = {n: m.tool_call_id or "" for n, m in enumerate(candidates, 1)}
    out: dict[str, str] = {}
    for key, note in (data.get("notes") or {}).items():
        n = int(key)
        if n in ids and str(note).strip():
            out[ids[n]] = " ".join(str(note).split())[:NOTE_CHARS]
    for n in data.get("keep") or []:
        if int(n) in ids:
            out[ids[int(n)]] = "keep"
    return out


async def decide(
    caller: Any,
    messages: Sequence[Message],
    candidates: Sequence[Message],
    settings: DecisionSettings,
    *,
    session_id: str = "",
    router: Any = None,
) -> dict[str, str]:
    """Ask the decision model about ``candidates``; {tool call id: "keep" | note}. Raises on any failure."""
    res = await caller.complete(
        TaskKind.CONTEXT_SELECT,
        [
            Message(role="system", content=SELECT_SYSTEM),
            Message(role="user", content=build_prompt(messages, candidates, settings.max_input_chars)),
        ],
        session_id=session_id,
        max_tokens=1024,
        timeout=settings.timeout,
        router=router,
    )
    return parse_decision(res.text, candidates)
