"""Crash-safe tool journal.

Before a tool runs, append an ``intent`` record to
``$K3CODE_HOME/journal/<session>.jsonl`` (fsync'd); after it runs, append a
``done`` record with a digest of the result. On resume after a crash, every
``intent`` without a matching ``done`` is replayed:

- pure tools (``side_effect=False``) may be re-run;
- side-effectful tools must NOT be re-run — the caller gets an ``INTERRUPTED``
  synthesis telling the model to inspect state before retrying.

Record shapes (one JSON object per line)::

    {"type": "intent", "id": "c1", "session": "s", "tool": "bash",
     "args_hash": "…", "side_effect": true, "ts": 1728000000.0}
    {"type": "done", "id": "c1", "digest": "…", "ts": …}
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Message embedded in synthesized tool results for interrupted side-effect tools.
INTERRUPTED_TEMPLATE = (
    "INTERRUPTED: {tool} may or may not have completed before a crash; "
    "inspect state (e.g. git diff / file contents) before retrying."
)


def args_digest(args: dict[str, Any]) -> str:
    """Stable digest of normalized tool args (sorted keys, compact JSON)."""
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


def result_digest(result: Any) -> str:
    """Stable digest of a tool result."""
    return hashlib.sha256(
        json.dumps(result, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


@dataclass
class JournalRecord:
    """One parsed journal line."""

    type: str  # "intent" | "done"
    id: str
    ts: float = 0.0
    tool: str = ""
    args_hash: str = ""
    side_effect: bool = False


@dataclass
class PendingIntent:
    """An intent found without a matching done on resume."""

    id: str
    tool: str
    args: dict[str, Any]  # only available when the caller recorded raw args
    side_effect: bool
    args_hash: str = ""


class ToolJournal:
    """Append-only, fsync'd JSONL journal of tool intents/completions."""

    def __init__(self, home: Path, session: str) -> None:
        self.session = session
        self.dir = home / "journal"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{session}.jsonl"
        self._fh = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        if self._fh and not self._fh.closed:
            self._fh.close()

    # ── append ──

    def record_intent(
        self,
        call_id: str,
        tool: str,
        args: dict[str, Any],
        side_effect: bool,
        *,
        raw_args: bool = True,
    ) -> float:
        """Append + fsync an ``intent`` record before the tool runs."""
        h = args_digest(args)
        rec = {
            "type": "intent",
            "id": call_id,
            "session": self.session,
            "tool": tool,
            "args_hash": h,
            "side_effect": side_effect,
            "ts": time.time(),
        }
        if raw_args:
            # Store args for pure tools so they can be re-run verbatim on resume.
            rec["args"] = args
        self._append(rec)
        return rec["ts"]

    def record_done(self, call_id: str, result: Any) -> None:
        """Append a ``done`` record with a result digest."""
        self._append(
            {
                "type": "done",
                "id": call_id,
                "digest": result_digest(result),
                "ts": time.time(),
            }
        )

    def _append(self, rec: dict[str, Any]) -> None:
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    # ── resume ──

    def read_all(self) -> list[JournalRecord]:
        """Read every record in the journal file."""
        out: list[JournalRecord] = []
        if not self.path.is_file():
            return out
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn write at crash: skip the partial line
                out.append(
                    JournalRecord(
                        type=d.get("type", ""),
                        id=d.get("id", ""),
                        ts=float(d.get("ts", 0.0)),
                        tool=d.get("tool", ""),
                        args_hash=d.get("args_hash", ""),
                        side_effect=bool(d.get("side_effect", False)),
                    )
                )
        return out

    def find_pending(self, records: list[JournalRecord]) -> list[JournalRecord]:
        """Intents with no later ``done`` for the same id."""
        seen_done: set[str] = set()
        for r in records:
            if r.type == "done":
                seen_done.add(r.id)
        pending = []
        for r in records:
            if r.type == "intent" and r.id not in seen_done:
                pending.append(r)
        return pending

    def load_args(self, records: list[JournalRecord]) -> dict[str, dict[str, Any]]:
        """Map intent id → raw args, for pure-tool re-runs."""
        args_by_id: dict[str, dict[str, Any]] = {}
        if not self.path.is_file():
            return args_by_id
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "intent" and isinstance(d.get("args"), dict):
                    args_by_id[d.get("id", "")] = d["args"]
        return args_by_id

    def resume_plan(self) -> list[PendingIntent]:
        """Compute the resume plan: pending intents with raw args where recorded."""
        records = self.read_all()
        pending = self.find_pending(records)
        args_by_id = self.load_args(records)
        return [
            PendingIntent(
                id=r.id,
                tool=r.tool,
                args=args_by_id.get(r.id, {}),
                side_effect=r.side_effect,
                args_hash=r.args_hash,
            )
            for r in pending
        ]


def interrupted_result(tool: str) -> dict[str, Any]:
    """Synthesized tool result for a side-effect tool that may have half-run."""
    return {"error": INTERRUPTED_TEMPLATE.format(tool=tool), "interrupted": True}
