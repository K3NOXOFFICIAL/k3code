"""Proactive proposals: a cheap-tier pass that suggests 0-3 follow-ups, kept in ``proposals.jsonl``."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from k3code.paths import private_file
from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

KINDS = ("consequence", "also_setup", "improvement")
#: M5 kinds created by the learning package (not by the LLM proposer); accepting them runs a handler.
LEARNED_KINDS = (
    "permission_rule",
    "preference",
    "project_setup",
    "skill",
    "optimizer",
    "project_gotcha",
    # project recipes (k3code.learning.recipes; payload op "recipe"): "skill" above doubles as one
    "mcp",
    "hook",
    "rule",
    "commands",
)

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
    #: M5: structured data for kinds the gateway applies itself (permission_rule, skill, optimizer, ...)
    payload: dict[str, Any] = field(default_factory=dict)
    project: str = ""

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


def _copy(p: Proposal) -> Proposal:
    return replace(p, payload=dict(p.payload) if isinstance(p.payload, dict) else {})


class _Snapshot:
    """The parsed file: last line per id, the keys seen and the highest ``p<N>``; valid while ``sig`` matches."""

    def __init__(self, sig: tuple[int, int, int]) -> None:
        self.sig = sig
        self.latest: dict[str, Proposal] = {}
        self.keys: set[str] = set()
        self.top = 0

    def put(self, p: Proposal) -> None:
        self.latest[p.id] = _copy(p)  # callers get copies (set_status mutates its own)
        self.keys.add(p.key)
        if p.id[1:].isdigit():
            self.top = max(self.top, int(p.id[1:]))


class ProposalStore:
    """jsonl, one line per proposal: the last line per id wins. New proposals are appended; a status change rewrites
    the file so it holds no superseded snapshots (older files may still carry some).

    The daemon, a standalone gateway and the CLI share the file. ``add`` and ``set_status`` read, check and write under
    an exclusive lock on ``proposals.jsonl.lock``, so two processes never hand out one id (the later line replaced the
    earlier proposal, and accepting a card applied another proposal's payload). The file and the lock are 0600 (what the
    owner approved). The parsed file is kept until its size, inode or mtime changes: mining checks hundreds of
    candidates, each of which used to re-read the whole file."""

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / "proposals.jsonl"
        self._snap: _Snapshot | None = None
        self._mutex = threading.RLock()  # the parsed file is shared by the threads of this process

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        lock = private_file(self.path.with_name(self.path.name + ".lock"))
        with self._mutex:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # releases the lock

    def _sig(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    def _load(self) -> _Snapshot:
        sig = self._sig()
        if sig is None:
            self._snap = None
            return _Snapshot((0, 0, 0))
        if self._snap is not None and self._snap.sig == sig:
            return self._snap
        snap = _Snapshot(sig)
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                p = Proposal(**json.loads(line))
            except (ValueError, TypeError):
                continue  # a torn or corrupt line must not break every read
            if isinstance(p.id, str) and isinstance(p.key, str):
                snap.put(p)
        self._snap = snap
        return snap

    @staticmethod
    def _line(p: Proposal) -> bytes:
        return (json.dumps(p.as_dict(), ensure_ascii=False) + "\n").encode("utf-8", "replace")

    def _write(self, p: Proposal) -> Proposal:
        """Append ``p``; returns it as stored (a lone surrogate in its text is a ``?`` on disk, so in memory too)."""
        before = self._snap
        line = self._line(p)
        stored = Proposal(**json.loads(line))
        with private_file(self.path).open("ab") as f:  # what the owner approved: 0600
            f.write(line)
        sig = self._sig()
        if before is not None and sig is not None and before.sig[0] == sig[0] and before.sig[1] + len(line) == sig[1]:
            before.sig = sig  # nothing but our line was appended: the parsed file is still right
            before.put(stored)
        else:
            self._snap = None
        return stored

    def all(self) -> list[Proposal]:
        with self._mutex:
            return [_copy(p) for p in self._load().latest.values()]

    def get(self, pid: str) -> Proposal | None:
        with self._mutex:
            p = self._load().latest.get(pid)
            return _copy(p) if p is not None else None

    def add(
        self,
        kind: str,
        text: str,
        action: str,
        session: str = "",
        *,
        payload: dict[str, Any] | None = None,
        project: str = "",
        key: str | None = None,
    ) -> Proposal | None:
        """Store a new proposal; None when its dedup key was seen before (pending, accepted or dismissed)."""
        key = key or dedup_key(kind, text)
        with self._locked():
            snap = self._load()
            if key in snap.keys:
                return None
            n = max(len(snap.latest), snap.top)  # skipped lines keep ids
            p = Proposal(
                id=f"p{n + 1}",
                kind=kind,
                text=text,
                action=action,
                key=key,
                session=session,
                ts=time.time(),
                payload=payload or {},
                project=project,
            )
            return self._write(p)

    def dismissed(self, key: str) -> bool:
        """Whether the proposal with this dedup key was dismissed."""
        return any(p.key == key and p.status == "dismissed" for p in self.all())

    def set_status(self, pid: str, status: str) -> Proposal | None:
        with self._locked():
            p = self.get(pid)
            if p is None:
                return None
            p.status = status
            self._rewrite([p if q.id == pid else q for q in self.all()])
            return self.get(pid)  # as stored

    def _rewrite(self, proposals: list[Proposal]) -> None:
        """Replace the file by one line per proposal, so status changes do not pile up superseded snapshots. Called
        with the lock held; the replacement is atomic (a reader sees the old file or the new one) and 0600."""
        tmp = private_file(self.path.with_name(self.path.name + ".tmp"))
        tmp.write_bytes(b"".join(self._line(q) for q in proposals))
        tmp.replace(self.path)
        self._snap = None


async def propose(
    caller: Any,
    store: ProposalStore,
    context: str,
    *,
    session_id: str = "",
    ranker: Any = None,
    preferences: list[str] | None = None,
    project: str = "",
) -> list[Proposal]:
    """Run the proposer on ``context`` and return the newly stored proposals (never raises).

    ``ranker(items) -> items`` (M5) orders/filters suggestions from decision history; ``preferences`` are the
    top learned preferences handed to the proposer prompt.
    """
    system = PROPOSER_SYSTEM
    if preferences:
        system += "\nLearned user preferences (bias suggestions toward these):\n" + "\n".join(
            f"- {p}" for p in preferences[:5]
        )
    try:
        res = await caller.complete(
            TaskKind.PROPOSAL,
            [Message(role="system", content=system), Message(role="user", content=context[-6000:])],
            session_id=session_id,
            escalate=False,  # best effort: an outage of the cheap tier is not worth a call on the main one
            max_tokens=500,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - proposals are a nicety
        return []
    out: list[Proposal] = []
    items = parse_proposals(res.text)
    if ranker is not None:
        items = ranker(items)
    for item in items:
        p = store.add(item["kind"], item["text"], item["action"], session_id, project=project)
        if p is not None:
            out.append(p)
    return out


def format_proposals(items: list[Proposal]) -> str:
    if not items:
        return "No proposals."
    mark = {"pending": "•", "accepted": "✓", "dismissed": "✗"}
    return "\n".join(f"{mark.get(p.status, '•')} {p.id} [{p.kind}] {p.text}  ({p.status})" for p in items)
