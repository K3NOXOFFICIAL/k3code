# Design ported from hermes-agent agent/background_review.py (MIT): after a long-enough session a cheap-tier pass
# extracts durable facts and reusable procedures; procedures become *drafts* that need acceptance.
"""Background review: durable facts → project memory (+mem0); procedures → skill drafts → proposals."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.learning import distiller
from k3code.memory import MAX_FACT_CHARS as MAX_FACT_CHARS  # the cap lives in memory, which re-applies it on read
from k3code.memory import clean_fact
from k3code.paths import home
from k3code.providers.types import Message
from k3code.redact import scrub_text
from k3code.routing.tiers import TaskKind

#: The heading earlier versions wrote into the repo's K3CODE.md/AGENTS.md; facts now go to learned.md (k3code.memory).
FACTS_HEADING = "## Learned project notes (auto)"
SYSTEM = (
    "You review a finished coding-agent session. Reply with ONE JSON object: "
    '{"facts": [str], "skills": [{"name": kebab-case, "description": str, "body": markdown steps}]}. '
    "facts = durable, project-specific facts worth remembering (commands, conventions, gotchas), max 5, one line each. "
    "skills = reusable multi-step procedures worth saving, max 2. Never include secrets, credentials or file "
    "contents. Return empty lists when nothing qualifies."
)
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def user_turns(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if m.get("role") == "user")


def transcript(messages: list[dict[str, Any]], limit: int = 7000) -> str:
    lines = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and m.get("role") in ("user", "assistant"):
            lines.append(f"{m['role']}: {c[:600]}")
    return "\n".join(lines)[-limit:]


def parse(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def write_draft(name: str, description: str, body: str) -> Path:
    d = home() / "skills" / "_drafts" / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n", encoding="utf-8")
    return md


async def review_session(
    caller: Any,
    messages: list[dict[str, Any]],
    *,
    store: ProposalStore,
    cwd: Path,
    config: Any = None,
    min_turns: int = 6,
    session_id: str = "",
    mem0_post: Any = None,
    project: str = "",
) -> dict[str, Any]:
    """Returns {"skipped": bool, "facts": [...], "drafts": [paths], "proposals": [...]}."""
    if user_turns(messages) <= min_turns:
        return {"skipped": True, "facts": [], "drafts": [], "proposals": []}
    try:
        res = await caller.complete(
            TaskKind.CLASSIFICATION,
            [Message(role="system", content=SYSTEM), Message(role="user", content=transcript(messages))],
            session_id=session_id,
            escalate=False,  # best effort: an outage of the cheap tier is not worth a call on the main one
            max_tokens=900,
            timeout=45,
        )
    except Exception:  # noqa: BLE001
        return {"skipped": True, "facts": [], "drafts": [], "proposals": []}
    data = parse(res.text)
    facts = [f for f in (clean_fact(str(x)) for x in data.get("facts") or []) if f][:5]
    if facts:
        # The summary can echo fetched web content, so facts never go into the repository's tracked memory file
        # (that re-injects them as project instructions for everyone): they go to $K3CODE_HOME, and the prompt shows
        # them fenced as auto-generated notes (k3code.memory).
        from k3code.memory import read_learned, write_learned

        merged = list(dict.fromkeys([*read_learned(cwd), *facts]))[-30:]
        write_learned(cwd, merged)
        # store_mem0 is blocking HTTP (10 s per fact): never on the gateway's event loop
        await asyncio.to_thread(
            distiller.store_mem0,
            config,
            [distiller.Preference(f, 0.6, 1) for f in facts],
            mem0_post,
            home() / "learning" / distiller.MEM0_POSTED,  # a fact seen again in a later review is not posted again
        )
    drafts: list[str] = []
    props: list[Proposal] = []
    for s in data.get("skills") or []:
        if not isinstance(s, dict) or not _NAME.match(str(s.get("name", ""))) or not str(s.get("body", "")).strip():
            continue
        if (home() / "skills" / s["name"]).exists():
            continue
        body = scrub_text(str(s["body"]))
        md = write_draft(s["name"], scrub_text(str(s.get("description", ""))).replace("\n", " "), body)
        drafts.append(str(md))
        label = f"Save skill '{s['name']}'? ({str(s.get('description', ''))[:80]})"
        payload = {"op": "save", "name": s["name"], "draft": str(md)}
        p = store.add(
            "skill",
            label,
            f"save skill {s['name']}",
            session_id,
            payload=payload,
            project=project,
            key=dedup_key("skill", f"save {s['name']}"),
        )
        if p:
            props.append(p)
    return {"skipped": False, "facts": facts, "drafts": drafts, "proposals": props}
