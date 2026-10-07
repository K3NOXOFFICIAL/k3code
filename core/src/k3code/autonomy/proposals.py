"""Proactive proposals: a cheap-tier pass that suggests 0-3 follow-ups, kept in ``proposals.jsonl``."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

KINDS = ("consequence", "also_setup", "improvement")

PROPOSER_SYSTEM = (
    "You think one step ahead of a coding agent. Given the plan or the finished task, reply with ONE JSON "
    'array of 0 to 3 suggestions: [{"kind": "consequence|also_setup|improvement", "text": str, "action": str}]. '
    "consequence = 'this action could lead to...'; also_setup = 'do you want me to also set up...'; "
    "improvement = 'you could also improve...'. `action` is a prompt the user could send to do it. "
    "Return [] if nothing is worth saying. No prose."
)


def dedup_key(kind: str, text: str) -> str:
    norm = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha1(f"{kind}:{norm}".encode()).hexdigest()[:12]


@dataclass
class Proposal:
    id: str
    kind: str
    text: str
    action: str
    key: str
    status: str = "pending"  # pending | accepted | dismissed
    session: str = ""
    ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_proposals(text: str) -> list[dict[str, str]]:
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out: list[dict[str, str]] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("kind") not in KINDS or not str(item.get("text", "")).strip():
            continue
        text_ = str(item["text"]).strip()
        out.append({"kind": item["kind"], "text": text_, "action": str(item.get("action") or text_).strip()})
    return out[:3]


class ProposalStore:
    """Append-only jsonl: each line is a proposal snapshot; the last line per id wins."""

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / "proposals.jsonl"

    def _write(self, p: Proposal) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(p.as_dict(), ensure_ascii=False) + "\n")

    def all(self) -> list[Proposal]:
        if not self.path.is_file():
            return []
        latest: dict[str, Proposal] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                p = Proposal(**json.loads(line))
                latest[p.id] = p
        return list(latest.values())

    def get(self, pid: str) -> Proposal | None:
        return next((p for p in self.all() if p.id == pid), None)

    def add(self, kind: str, text: str, action: str, session: str = "") -> Proposal | None:
        """Store a new proposal; None when its dedup key was seen before (pending, accepted or dismissed)."""
        existing = self.all()
        key = dedup_key(kind, text)
        if any(p.key == key for p in existing):
            return None
        p = Proposal(id=f"p{len(existing) + 1}", kind=kind, text=text, action=action, key=key,
                     session=session, ts=time.time())
        self._write(p)
        return p

    def set_status(self, pid: str, status: str) -> Proposal | None:
        p = self.get(pid)
        if p is None:
            return None
        p.status = status
        self._write(p)
        return p


async def propose(caller: Any, store: ProposalStore, context: str, *, session_id: str = "") -> list[Proposal]:
    """Run the proposer on ``context`` and return the newly stored proposals (never raises)."""
    try:
        res = await caller.complete(
            TaskKind.CLASSIFICATION,
            [Message(role="system", content=PROPOSER_SYSTEM), Message(role="user", content=context[-6000:])],
            session_id=session_id,
            max_tokens=500,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - proposals are a nicety
        return []
    out: list[Proposal] = []
    for item in parse_proposals(res.text):
        p = store.add(item["kind"], item["text"], item["action"], session_id)
        if p is not None:
            out.append(p)
    return out


def format_proposals(items: list[Proposal]) -> str:
    if not items:
        return "No proposals."
    mark = {"pending": "•", "accepted": "✓", "dismissed": "✗"}
    return "\n".join(f"{mark.get(p.status, '•')} {p.id} [{p.kind}] {p.text}  ({p.status})" for p in items)
