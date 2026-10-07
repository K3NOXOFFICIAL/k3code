"""Usage accounting: one SQLite event table at ``$K3CODE_HOME/usage.db`` plus aggregation for ``/stats``.

Rows are appended from router/loop events (model calls with token counts,
failovers, retries, pauses with the time they lasted, tool calls, approval
prompts). ``cost_usd`` stays NULL unless a price is known; stats then report
cost as unknown instead of inventing a number.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    session TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    seconds REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT '',
    task_kind TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_day ON events(day);
CREATE INDEX IF NOT EXISTS events_session ON events(session);
"""

#: Event kinds written to the table.
KINDS = ("call", "failover", "retry", "pause", "tool", "approval")


class UsageDB:
    """Append-only usage events with ``by`` = ``session`` | ``day`` aggregation."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.executescript(_SCHEMA)
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(events)")}
        for col in ("tier", "task_kind"):  # M4a: databases from M2 lack these
            if col not in cols:
                self._db.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        self._db.commit()

    def record(
        self,
        kind: str,
        *,
        session: str = "",
        provider: str = "",
        model: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float | None = None,
        seconds: float = 0.0,
        detail: str = "",
        ts: float | None = None,
        tier: str = "",
        task_kind: str = "",
    ) -> None:
        ts = time.time() if ts is None else ts
        self._db.execute(
            "INSERT INTO events (ts, day, session, kind, provider, model, tokens_in, tokens_out, cost_usd,"
            " seconds, detail, tier, task_kind)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ts,
                time.strftime("%Y-%m-%d", time.localtime(ts)),
                session,
                kind,
                provider,
                model,
                tokens_in,
                tokens_out,
                cost_usd,
                seconds,
                detail[:500],
                tier,
                task_kind,
            ),
        )
        self._db.commit()

    def rows(self, since: float = 0.0) -> list[dict[str, Any]]:
        """Raw events with ``ts >= since`` as dicts (for the self-optimizer)."""
        cur = self._db.execute(
            "SELECT ts, session, kind, provider, model, detail, tier, task_kind FROM events WHERE ts >= ? ORDER BY ts",
            (since,))
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur]

    def aggregate(
        self, by: str = "day", *, session: str | None = None, days: int | None = None
    ) -> list[dict[str, Any]]:
        """One dict per session or day (newest first) with counters and per-model call counts."""
        if by not in ("session", "day"):
            raise ValueError("by must be 'session' or 'day'")
        where, args = [], []
        if session:
            where.append("session = ?")
            args.append(session)
        if days:
            where.append("ts >= ?")
            args.append(time.time() - days * 86400)
        sql = (
            "SELECT ts, day, session, kind, provider, model, tokens_in, tokens_out, cost_usd, seconds, tier,"
            " task_kind FROM events"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        groups: dict[str, dict[str, Any]] = {}
        calls: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        rows = self._db.execute(sql + " ORDER BY ts", args)
        for ts, day, sess, kind, provider, model, t_in, t_out, cost, secs, tier, task_kind in rows:
            key = day if by == "day" else sess
            g = groups.setdefault(
                key,
                {
                    "key": key,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "cost_usd": None,
                    "calls": 0,
                    "failovers": 0,
                    "retries": 0,
                    "pauses": 0,
                    "paused_seconds": 0.0,
                    "tool_calls": 0,
                    "approvals": 0,
                    "last_ts": 0.0,
                    "by_model": {},
                    "by_tier": {},
                    "by_kind": {},
                },
            )
            g["last_ts"] = max(g["last_ts"], ts)
            g["tokens_in"] += t_in
            g["tokens_out"] += t_out
            if cost is not None:
                g["cost_usd"] = (g["cost_usd"] or 0.0) + cost
            if kind == "call":
                g["calls"] += 1
                calls[key][f"{provider}/{model}"] += 1
                if tier:
                    t = g["by_tier"].setdefault(tier, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
                    t["calls"] += 1
                    t["tokens_in"] += t_in
                    t["tokens_out"] += t_out
                if task_kind:
                    g["by_kind"][task_kind] = g["by_kind"].get(task_kind, 0) + 1
            elif kind == "failover":
                g["failovers"] += 1
            elif kind == "retry":
                g["retries"] += 1
            elif kind == "pause":
                g["pauses"] += 1
                g["paused_seconds"] += secs
            elif kind == "tool":
                g["tool_calls"] += 1
            elif kind == "approval":
                g["approvals"] += 1
        for key, g in groups.items():
            g["by_model"] = dict(calls[key])
        return sorted(groups.values(), key=lambda g: g["last_ts"], reverse=True)

    def close(self) -> None:
        self._db.close()


def format_stats(rows: list[dict[str, Any]], by: str) -> str:
    """Plain-text table for the CLI / chat."""
    if not rows:
        return "No usage recorded yet."
    out = [f"Usage per {by}:"]
    for g in rows:
        cost = "unknown" if g["cost_usd"] is None else f"${g['cost_usd']:.4f}"
        models = ", ".join(f"{m}×{n}" for m, n in sorted(g["by_model"].items())) or "-"
        tiers = ", ".join(
            f"{t} {v['calls']} calls ({v['tokens_in']}/{v['tokens_out']} tok)" for t, v in sorted(g["by_tier"].items())
        )
        out.append(
            f"- {g['key'] or '(none)'}: tokens {g['tokens_in']}/{g['tokens_out']} in/out, cost {cost}, "
            f"calls {g['calls']} [{models}], failovers {g['failovers']}, retries {g['retries']}, "
            f"pauses {g['pauses']} ({g['paused_seconds']:.0f}s paused), tools {g['tool_calls']}, "
            f"approvals {g['approvals']}"
        )
        if tiers:
            out.append(f"    tiers: {tiers}")
        if g["by_kind"]:
            out.append("    kinds: " + ", ".join(f"{k}×{n}" for k, n in sorted(g["by_kind"].items())))
    return "\n".join(out)
