"""The append-only full-text recall index of every stored session, inside ``sessions.db``.

``message_archive`` keeps each user and assistant text and each compaction summary a session ever saved, scrubbed of
credentials, one row per distinct text (``UNIQUE(session_id, digest)``). It is fed at the session store's write choke
points (:func:`ingest`, inside the save's own transaction), before compaction and ``/clear`` rewrite a transcript,
and by a background backfill of the sessions saved before the index existed (:func:`backfill_batch`). Rows are never
deleted on a save: what compaction folds and ``/clear`` drops stays searchable. Deleting a session deletes its rows.

``message_fts`` is an external-content FTS5 table over the archive, kept in step by triggers. A Python build without
FTS5 keeps the archive and :func:`search` falls back to a substring scan.

``archive_state`` is each session's high-water mark: how many list entries are archived (``indexed``), a digest of the
last of them (``tail``) and the offset of the list's positions in ``seq`` (``base``). A save whose list still ends the
same way at ``indexed`` archives only the new entries; any other list (compaction, ``/clear``, an import) is archived
whole, deduplicated by digest, at positions after every earlier row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from k3code.redact import scrub_text
from k3code.session_ai import SUMMARY_PREFIX

logger = logging.getLogger(__name__)

#: Longest text archived per message; the rest of it is not searchable.
MAX_TEXT_CHARS = 20_000
#: Candidate rows ranked in Python per query.
CANDIDATES = 200
MAX_TERMS = 12
SNIPPET_CHARS = 280
KIND_WEIGHT = {"summary": 1.2, "user": 1.0, "assistant": 1.0}
#: Score factor for a hit in the caller's current project.
PROJECT_BOOST = 1.5

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS message_archive (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    ts REAL NOT NULL,
    cwd TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(session_id, digest))""",
    "CREATE INDEX IF NOT EXISTS message_archive_sid ON message_archive (session_id, seq)",
    """CREATE TABLE IF NOT EXISTS archive_state (
    session_id TEXT PRIMARY KEY,
    indexed INTEGER NOT NULL,
    tail TEXT NOT NULL,
    at REAL NOT NULL,
    base INTEGER NOT NULL DEFAULT 0)""",
)
_FTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(text, content='message_archive', content_rowid='id',"
    " tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER IF NOT EXISTS message_archive_ai AFTER INSERT ON message_archive BEGIN"
    " INSERT INTO message_fts(rowid, text) VALUES (new.id, new.text); END",
    "CREATE TRIGGER IF NOT EXISTS message_archive_ad AFTER DELETE ON message_archive BEGIN"
    " INSERT INTO message_fts(message_fts, rowid, text) VALUES ('delete', old.id, old.text); END",
)

# a small en + de list: a query of only these finds nothing rather than everything
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "she",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "aber",
        "als",
        "am",
        "an",
        "auch",
        "auf",
        "aus",
        "bei",
        "bin",
        "bis",
        "da",
        "das",
        "dass",
        "dein",
        "dem",
        "den",
        "der",
        "des",
        "die",
        "dir",
        "doch",
        "du",
        "ein",
        "eine",
        "einem",
        "einen",
        "einer",
        "es",
        "für",
        "hab",
        "habe",
        "hat",
        "ich",
        "ihr",
        "im",
        "ist",
        "ja",
        "kann",
        "mein",
        "mich",
        "mir",
        "mit",
        "nach",
        "nicht",
        "noch",
        "nur",
        "ob",
        "oder",
        "sie",
        "sind",
        "so",
        "und",
        "uns",
        "von",
        "war",
        "was",
        "wenn",
        "wer",
        "wie",
        "wir",
        "wo",
        "zu",
        "zum",
        "zur",
    ]
)


@dataclass
class Hit:
    """One archived message that matched. ``at`` is when the text was archived; for a session indexed by the backfill
    (saved before the index existed) it is the session's last save, so only an approximate date."""

    id: str  # f"{session_id}:{seq}", what read_around takes
    session_id: str
    seq: int
    title: str
    cwd: str
    at: float
    role: str  # 'user' | 'assistant' (a summary is a user message)
    kind: str  # 'user' | 'assistant' | 'summary'
    snippet: str
    score: float
    matched_terms: int


@dataclass
class ArchivedMessage:
    id: str
    session_id: str
    seq: int
    role: str
    kind: str
    at: float
    text: str


def ensure_schema(db: sqlite3.Connection) -> bool:
    """Create the archive tables (DDL only, idempotent); True when the FTS5 index is there too. Without FTS5 the
    archive still fills and :func:`search` scans it instead."""
    for stmt in _SCHEMA:
        db.execute(stmt)
    try:
        had_fts = _has_fts(db)
        for stmt in _FTS:
            db.execute(stmt)
        if not had_fts and db.execute("SELECT 1 FROM message_archive LIMIT 1").fetchone():
            # archive rows written by a build without FTS5: index them once
            db.execute("INSERT INTO message_fts(message_fts) VALUES ('rebuild')")
    except sqlite3.OperationalError as exc:  # no such module: fts5
        logger.warning("recall index: full-text search unavailable (%s); searching by substring instead", exc)
        db.commit()
        return False
    db.commit()
    return True


def _has_fts(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name = 'message_fts'").fetchone() is not None


def _digest(kind: str, text: str) -> str:
    return hashlib.sha1(f"{kind}\0{text}".encode(), usedforsecurity=False).hexdigest()[:16]


def _boundary(message: Any) -> str:
    """A digest of one whole list entry (tool calls and results included): where the archived part of a list ends."""
    raw = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass"), usedforsecurity=False).hexdigest()[:16]


def _clean(role: Any, text: Any) -> tuple[str, str] | None:
    """``(kind, text)`` of a user or assistant text as archived (summary prefix cut, capped, scrubbed), else None.
    Shared by the live path and the backfill, so both store the same digest for the same message."""
    if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
        return None
    kind = role
    if role == "user" and text.startswith(SUMMARY_PREFIX):
        kind, text = "summary", text[len(SUMMARY_PREFIX) :]
    text = scrub_text(text[: MAX_TEXT_CHARS + 512])[:MAX_TEXT_CHARS].strip()
    return (kind, text) if text else None


def extract_text(message: Any) -> tuple[str, str] | None:
    """``(kind, text)`` to archive for one transcript entry, or None. Only user and assistant text (a list content
    joins the ``text`` of its parts) and compaction summaries (kind ``summary``); never tool results or tool calls."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return _clean(message.get("role"), content)


def _state(db: sqlite3.Connection, session_id: str) -> tuple[int, str, int] | None:
    row = db.execute("SELECT indexed, tail, base FROM archive_state WHERE session_id = ?", (session_id,)).fetchone()
    return (row[0], row[1], row[2]) if row else None


def _next_seq(db: sqlite3.Connection, session_id: str) -> int:
    row = db.execute("SELECT MAX(seq) FROM message_archive WHERE session_id = ?", (session_id,)).fetchone()
    return 0 if row[0] is None else int(row[0]) + 1


_INSERT = "INSERT OR IGNORE INTO message_archive (session_id, seq, role, ts, cwd, digest, text) VALUES (?,?,?,?,?,?,?)"


def ingest(db: sqlite3.Connection, session_id: str, cwd: str, messages: Sequence[Any], at: float) -> int:
    """Archive what ``messages`` (the session's whole current list) adds; returns the rows inserted. No commit: the
    caller's transaction holds it.

    When the archived part still ends the same way, only the entries after it are read. Otherwise (compaction, /clear,
    an import, a list written by an older version) every entry is offered again, and the digest keeps each text once;
    the new ones get positions after all earlier rows. Nothing is ever deleted here."""
    state = _state(db, session_id)
    indexed, tail, base = state if state else (0, "", 0)
    if state is not None and indexed == -1:  # the backfill could not read this row; a save rewrote it since
        indexed = 0
    if state is None or not (indexed <= len(messages) and (indexed == 0 or _boundary(messages[indexed - 1]) == tail)):
        start, base = 0, _next_seq(db, session_id)
    else:
        start = indexed
    rows = []
    for i in range(start, len(messages)):
        got = extract_text(messages[i])
        if got is not None:
            kind, text = got
            rows.append((session_id, base + i, kind, at, cwd, _digest(kind, text), text))
    inserted = db.executemany(_INSERT, rows).rowcount if rows else 0
    db.execute(
        "INSERT OR REPLACE INTO archive_state (session_id, indexed, tail, at, base) VALUES (?,?,?,?,?)",
        (session_id, len(messages), _boundary(messages[-1]) if messages else "", at, base),
    )
    return max(inserted, 0)


def delete_session(db: sqlite3.Connection, session_id: str) -> None:
    """Forget a session's archive (its FTS rows go with them, by trigger). No commit."""
    db.execute("DELETE FROM message_archive WHERE session_id = ?", (session_id,))
    db.execute("DELETE FROM archive_state WHERE session_id = ?", (session_id,))


# ── backfill ──────────────────────────────────────────────────────────

_EXTRACT = """
SELECT j.key,
  CASE WHEN j.type = 'object' THEN json_extract(j.value, '$.role') END,
  CASE WHEN j.type = 'object' THEN
    CASE json_type(j.value, '$.content')
      WHEN 'text' THEN json_extract(j.value, '$.content')
      WHEN 'array' THEN (
        SELECT group_concat(CASE WHEN p.type = 'object' AND json_type(p.value, '$.text') = 'text'
                                 THEN json_extract(p.value, '$.text') END, '')
        FROM json_each(j.value, '$.content') p)
    END
  END
FROM sessions s, json_each(s.messages) j WHERE s.session_id = ?
"""


def backfill_pending(db: sqlite3.Connection) -> int:
    """Stored sessions with messages that the archive has not seen yet."""
    return db.execute(
        "SELECT COUNT(*) FROM sessions s WHERE NOT EXISTS (SELECT 1 FROM archive_state a WHERE"
        " a.session_id = s.session_id) AND s.messages NOT IN ('', '[]')"
    ).fetchone()[0]


def archive_count(db: sqlite3.Connection) -> int:
    """Sessions the archive has indexed (live or by the backfill); unreadable rows not counted."""
    return db.execute("SELECT COUNT(*) FROM archive_state WHERE indexed >= 0").fetchone()[0]


def backfill_batch(db: sqlite3.Connection, max_sessions: int = 8, max_bytes: int = 4 << 20) -> int:
    """Archive up to ``max_sessions`` stored sessions that have no ``archive_state`` yet, newest first, stopping once
    ``max_bytes`` of transcript JSON was read (at least one session is). One transaction per session, so the write lock
    is held briefly. The transcript is taken apart by SQLite's ``json_each`` (no ``json.loads`` of the whole list);
    the texts are scrubbed in Python. A row whose JSON is unreadable is marked ``indexed = -1`` and never retried.
    Returns the sessions processed: 0 when there is nothing left."""
    ids = [
        r[0]
        for r in db.execute(
            "SELECT s.session_id FROM sessions s WHERE NOT EXISTS (SELECT 1 FROM archive_state a WHERE"
            " a.session_id = s.session_id) AND s.messages NOT IN ('', '[]') ORDER BY s.updated_at DESC LIMIT ?",
            (max(1, max_sessions),),
        )
    ]
    done = read = 0
    for sid in ids:
        if done and read >= max_bytes:
            break
        read += _backfill_one(db, sid)
        done += 1
    return done


def _backfill_one(db: sqlite3.Connection, sid: str) -> int:
    """Archive one session's stored transcript; returns its JSON size."""
    with db:
        row = db.execute(
            "SELECT length(messages), json_valid(messages), cwd, updated_at FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        if row is None or _state(db, sid) is not None:  # deleted, or a save indexed it meanwhile
            return 0
        size, valid, cwd, updated_at = row
        count = last = None
        if valid:  # a separate statement: SQLite may evaluate json_* on invalid JSON whatever the CASE says
            valid, count, last = db.execute(
                "SELECT json_type(messages) = 'array', json_array_length(messages), json_extract(messages, '$[#-1]')"
                " FROM sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
        if not valid:
            logger.warning("recall index: stored session %s has unreadable messages; not indexed", sid)
            db.execute(
                "INSERT OR REPLACE INTO archive_state (session_id, indexed, tail, at, base) VALUES (?,-1,'',?,0)",
                (sid, updated_at),
            )
            return size or 0
        base = _next_seq(db, sid)
        rows = []
        for key, role, text in db.execute(_EXTRACT, (sid,)):
            got = _clean(role, text)
            if got is not None:
                kind, clean = got
                rows.append((sid, base + int(key), kind, updated_at, cwd or "", _digest(kind, clean), clean))
        if rows:
            db.executemany(_INSERT, rows)
        tail = _boundary(json.loads(last)) if isinstance(last, str) and last.startswith("{") else _boundary(last)
        db.execute(
            "INSERT OR REPLACE INTO archive_state (session_id, indexed, tail, at, base) VALUES (?,?,?,?,?)",
            (sid, count or 0, tail if count else "", updated_at, base),
        )
    return size or 0


# ── search ────────────────────────────────────────────────────────────


def query_terms(query: str) -> list[str]:
    """The distinct words of ``query`` worth searching for: lower-cased, no stopwords, 2+ chars, at most 12."""
    out: list[str] = []
    for term in re.findall(r"\w+", query.lower()):
        if len(term) >= 2 and term not in STOPWORDS and term not in out:
            out.append(term)
    return out[:MAX_TERMS]


def match_expression(terms: Sequence[str]) -> str:
    """An FTS5 MATCH string built only from quoted terms (OR-joined, a prefix match for 4+ chars): raw query text
    would make ``-``, ``:``, ``*``, ``NEAR`` or a stray quote a syntax error."""
    quoted = ['"' + t.replace('"', '""') + '"' + ("*" if len(t) >= 4 else "") for t in terms]
    return " OR ".join(quoted)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _project_sql(column: str, project: str) -> tuple[str, list[Any]]:
    root = project.rstrip("/") or "/"
    return f"({column} = ? OR {column} LIKE ? ESCAPE '\\')", [root, _like_escape(root.rstrip("/")) + "/%"]


def _one_line(text: str, limit: int = SNIPPET_CHARS) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _window(text: str, terms: Sequence[str]) -> str:
    low = text.lower()
    found = [i for i in (low.find(t) for t in terms) if i >= 0]
    at = min(found) if found else 0
    start = max(0, at - 120)
    return ("…" if start else "") + text[start : at + 120] + ("…" if at + 120 < len(text) else "")


def search(
    db: sqlite3.Connection,
    query: str,
    *,
    project: str | None = None,
    current_project: str | None = None,
    since: float | None = None,
    session_id: str | None = None,
    limit: int = 8,
    per_session: int = 2,
    fts: bool | None = None,
) -> list[Hit]:
    """Archived messages of every stored session that match ``query``'s words, best first.

    ``project`` keeps only messages whose session cwd is that directory or below it; ``current_project`` (usually the
    caller's own) is not a filter but scores its hits x1.5. ``since`` is an epoch (``Hit.at >= since``). At most
    ``per_session`` hits per session. Ranking is bm25 weighted by kind (summary 1.2) and by how many distinct terms a
    message has; recency only breaks ties. ``fts=None`` uses FTS5 when the index has it, else a substring scan.
    A query with no searchable word returns []."""
    terms = query_terms(query)
    if not terms or limit <= 0:
        return []
    where: list[str] = []
    args: list[Any] = []
    if project:
        sql, more = _project_sql("a.cwd", project)
        where.append(sql)
        args += more
    if since is not None:
        where.append("a.ts >= ?")
        args.append(float(since))
    if session_id:
        where.append("a.session_id = ?")
        args.append(session_id)
    boost_sql, boost_args = ("0", []) if not current_project else _project_sql("a.cwd", current_project)
    use_fts = _has_fts(db) if fts is None else fts
    cols = "a.id, a.session_id, a.seq, a.role, a.ts, a.cwd, COALESCE(s.title, ''), a.text"
    if use_fts:
        weight = f"(CASE a.role WHEN 'summary' THEN 1.2 ELSE 1.0 END) * (CASE WHEN {boost_sql} THEN 1.5 ELSE 1.0 END)"
        sql = (
            f"SELECT {cols}, -bm25(message_fts) * {weight} AS rank,"
            " snippet(message_fts, 0, '[', ']', '…', 24)"
            " FROM message_fts JOIN message_archive a ON a.id = message_fts.rowid"
            " LEFT JOIN sessions s ON s.session_id = a.session_id"
            " WHERE message_fts MATCH ?" + "".join(f" AND {w}" for w in where) + " ORDER BY rank DESC LIMIT ?"
        )
        rows = db.execute(sql, [*boost_args, match_expression(terms), *args, CANDIDATES]).fetchall()
    else:
        any_term = " OR ".join("instr(lower(a.text), ?) > 0" for _ in terms)
        sql = (
            f"SELECT {cols}, (CASE a.role WHEN 'summary' THEN 1.2 ELSE 1.0 END)"
            f" * (CASE WHEN {boost_sql} THEN 1.5 ELSE 1.0 END) AS rank, NULL"
            " FROM message_archive a LEFT JOIN sessions s ON s.session_id = a.session_id"
            f" WHERE ({any_term})" + "".join(f" AND {w}" for w in where) + " ORDER BY a.ts DESC, a.id DESC LIMIT ?"
        )
        rows = db.execute(sql, [*boost_args, *terms, *args, CANDIDATES * 5]).fetchall()
    hits: list[Hit] = []
    for _rid, sid, seq, kind, ts, cwd, title, text, rank, snip in rows:
        low = text.lower()
        matched = max(1, sum(1 for t in terms if t in low))
        hits.append(
            Hit(
                id=f"{sid}:{seq}",
                session_id=sid,
                seq=seq,
                title=title,
                cwd=cwd,
                at=ts,
                role="user" if kind == "summary" else kind,
                kind=kind,
                snippet=_one_line(snip if snip else _window(text, terms)),
                score=float(rank) * (1 + 0.5 * (matched - 1)),
                matched_terms=matched,
            )
        )
    hits.sort(key=lambda h: (-h.score, -h.at))
    out: list[Hit] = []
    per: dict[str, int] = {}
    for h in hits:
        if per.get(h.session_id, 0) >= max(1, per_session):
            continue
        per[h.session_id] = per.get(h.session_id, 0) + 1
        out.append(h)
        if len(out) >= limit:
            break
    return out


def read_around(
    db: sqlite3.Connection, session_id: str, seq: int, before: int = 1, after: int = 4
) -> list[ArchivedMessage]:
    """The archived messages of ``session_id`` around position ``seq``: up to ``before`` earlier ones, the one at
    ``seq`` (or the next after it) and up to ``after`` later ones, in order. Reads the archive, never the stored
    transcript (whose positions move on compaction)."""
    cols = "id, session_id, seq, role, ts, text"
    earlier = db.execute(
        f"SELECT {cols} FROM message_archive WHERE session_id = ? AND seq < ? ORDER BY seq DESC LIMIT ?",  # noqa: S608
        (session_id, seq, max(0, before)),
    ).fetchall()
    later = db.execute(
        f"SELECT {cols} FROM message_archive WHERE session_id = ? AND seq >= ? ORDER BY seq LIMIT ?",  # noqa: S608
        (session_id, seq, max(0, after) + 1),
    ).fetchall()
    return [
        ArchivedMessage(f"{sid}:{sq}", sid, sq, "user" if kind == "summary" else kind, kind, ts, t)
        for _rid, sid, sq, kind, ts, t in [*reversed(earlier), *later]
    ]
