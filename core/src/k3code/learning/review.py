# Design ported from hermes-agent agent/background_review.py (MIT): after a long-enough session a cheap-tier pass
# extracts durable facts and reusable procedures; procedures become *drafts* that need acceptance.
"""Background review: durable facts → project memory (+mem0); procedures → skill drafts → proposals."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.learning import DEFAULTS, curator, distiller
from k3code.learning.ranking import jaccard, tokens
from k3code.memory import MAX_FACT_CHARS as MAX_FACT_CHARS  # the cap lives in memory, which re-applies it on read
from k3code.memory import clean_fact
from k3code.paths import ensure_private_dir, home, private_file
from k3code.providers.types import Message
from k3code.redact import scrub_text
from k3code.routing.tiers import TaskKind
from k3code.session_ai import SUMMARY_PREFIX

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

#: A project gets at most ``max_skill_cards`` new skill cards per this long, answered or not.
CARD_WINDOW = 7 * 86400.0

#: How much of the body the card itself shows (the TUI cuts it to the terminal width); ``/proposals show`` has it all.
PREVIEW_CHARS = 120


#: A compaction summary is the only record of the folded part of the conversation, so it is clipped less than a message.
#: It is at most 700 tokens (``compact_messages``); ``transcript``'s limit leaves room for it next to the kept tail.
SUMMARY_CLIP = 3000


def user_turns(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if m.get("role") == "user")


def session_turns(meta: dict[str, Any], messages: list[dict[str, Any]]) -> int:
    """User turns of the whole session, which the stored list stops telling once it is compacted.

    Compaction replaces the list with the task anchor, a summary and the last messages, so its own user messages fall
    from 60 to about 6. ``meta`` keeps how many user messages the list held when last asked and how many left it
    since (compaction, ``/clear``), and the total never falls. It follows the list, not the calls: the hook runs late
    and more than once per turn.
    """
    live = user_turns(messages)
    seen = int(meta.get("turns_in_list", 0))
    if live < seen:
        meta["turns_dropped"] = int(meta.get("turns_dropped", 0)) + seen - live
    meta["turns_in_list"] = live
    return int(meta.get("turns_dropped", 0)) + live


def transcript(messages: list[dict[str, Any]], limit: int = 9000) -> str:
    lines = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and m.get("role") in ("user", "assistant"):
            lines.append(f"{m['role']}: {c[: SUMMARY_CLIP if c.startswith(SUMMARY_PREFIX) else 600]}")
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
    ensure_private_dir(d)  # a draft is written from the session: 0700/0600, and it keeps both when accepted
    md = private_file(d / "SKILL.md")
    md.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n", encoding="utf-8")
    return md


def _is_save_card(p: Proposal) -> bool:
    return p.kind == "skill" and p.payload.get("op") == "save"


def _known_skills(existing: list[Proposal]) -> list[set[str]]:
    """Token sets of the live skills and of every skill a card was made for, dismissed ones included (the draft of
    a card that was answered is gone, so its body is kept in the card)."""
    out = [tokens(curator.skill_body(sk["text"])) for sk in curator.list_skills()]
    for p in existing:
        if _is_save_card(p):
            body = p.payload.get("body")
            if body is None:  # a card made before the body was kept in it
                body = curator.skill_body(curator.draft_text(p.payload) or "")
            out.append(tokens(str(body)))
    return out


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
    turns: int | None = None,
    ranker: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    max_skill_cards: int = DEFAULTS["skill_cards_per_week"],
    now: float | None = None,
) -> dict[str, Any]:
    """Returns {"skipped": bool, "facts": [...], "drafts": [paths], "proposals": [...]}.

    ``turns`` is the user turns of the whole session (``session_turns``); ``messages`` may be a compacted list that is
    shorter than the session.

    A skill becomes a card only when its body is not like a live skill or a skill an earlier card was made for (a
    reworded name is not a new skill), the ``ranker`` keeps it, and the project is under ``max_skill_cards`` per
    ``CARD_WINDOW``. A card whose name was seen never rewrites that draft.
    """
    if (user_turns(messages) if turns is None else turns) <= min_turns:
        return {"skipped": True, "facts": [], "drafts": [], "proposals": []}
    try:
        res = await caller.complete(
            TaskKind.CLASSIFICATION,
            [Message(role="system", content=SYSTEM), Message(role="user", content=transcript(messages))],
            session_id=session_id,
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
        # store_mem0 is blocking HTTP (10 s per fact) and project_scope may run git: never on the gateway's event
        # loop. The facts are about this project (commands, hosts, conventions), so mem0 gets them scoped to it.
        await asyncio.to_thread(
            lambda: distiller.store_mem0(
                config,
                [distiller.Preference(f, 0.6, 1) for f in facts],
                mem0_post,
                home() / "learning" / distiller.MEM0_POSTED,  # a fact seen again in a later review is not posted again
                distiller.project_scope(cwd, project),
            )
        )
    existing = store.all()
    known = _known_skills(existing)
    seen = {p.key for p in existing}
    since = (time.time() if now is None else now) - CARD_WINDOW
    made = sum(1 for p in existing if _is_save_card(p) and p.project == project and p.ts >= since)
    cands: list[dict[str, Any]] = []
    for s in data.get("skills") or []:
        if not isinstance(s, dict) or not _NAME.match(str(s.get("name", ""))) or not str(s.get("body", "")).strip():
            continue
        name = s["name"]
        key = dedup_key("skill", f"save {name}")
        if (home() / "skills" / name).exists() or key in seen:
            continue
        body = scrub_text(str(s["body"])).strip()
        mine = tokens(body)
        if any(jaccard(mine, k) >= curator.DUP_THRESHOLD for k in known):
            continue
        known.append(mine)
        seen.add(key)
        desc = scrub_text(str(s.get("description", ""))).replace("\n", " ")
        preview = " ".join(body.split())
        label = (
            f"Save skill '{name}'? ({desc[:80]}) {preview[:PREVIEW_CHARS]}{'…' if len(preview) > PREVIEW_CHARS else ''}"
        )
        cands.append(
            {
                "kind": "skill",
                "text": label,
                "action": f"save skill {name}",
                "key": key,
                "name": name,
                "desc": desc,
                "body": body,
            }
        )
    if ranker is not None:
        cands = ranker(cands)
    drafts: list[str] = []
    props: list[Proposal] = []
    for c in cands:
        if made >= max_skill_cards:
            break
        md = write_draft(c["name"], c["desc"], c["body"])
        drafts.append(str(md))
        payload = {"op": "save", "name": c["name"], "draft": str(md), "body": c["body"]}
        p = store.add("skill", c["text"], c["action"], session_id, payload=payload, project=project, key=c["key"])
        if p:
            props.append(p)
            made += 1
    return {"skipped": False, "facts": facts, "drafts": drafts, "proposals": props}
