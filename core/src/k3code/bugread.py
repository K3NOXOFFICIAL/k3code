"""Read side of the bug catcher: rows of ``bugs.jsonl`` and ``logs/tui_gateway_crash.log`` grouped by fingerprint.

:func:`group`, :func:`crash_rows`, :func:`parse_rows` and :func:`format_brief` are pure; :func:`groups`,
:func:`show`, :func:`resolve` and :func:`export` read and write ``$K3CODE_HOME``.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from k3code import bugcatch
from k3code.paths import ensure_private_dir, private_file

CRASH_LOG = Path("logs") / "tui_gateway_crash.log"
RESOLVED = "resolved"
HEADER = "Fix these k3code bugs. Write a failing test first for each."
_DAY = 86400.0

#: ``[tui-parent] <ISO time> <payload>``, as tui/src/lib/parentLog.ts writes it.
_CRASH_LINE = re.compile(r"^\[tui-parent\] (\S+) (.*)$")
_CRASH_SCOPE = re.compile(r"^(uncaughtException|unhandledRejection): ")
#: One gateway death also writes ``transport exit``; ``child exit`` alone counts it once.
_CHILD_EXIT = re.compile(r"^\[lifecycle\] child exit .*\bcode=([1-9]\d*)\b")
_TB_FRAME = re.compile(r'File "([^"]*k3code/[^"]+)", line (\d+)')


@dataclass(frozen=True)
class Group:
    fp: str
    kind: str
    count: int
    first_seen: float
    last_seen: float
    summary: str
    suppressed: int
    open: bool
    latest: dict[str, Any]


def parse_rows(text: str) -> list[dict[str, Any]]:
    """The rows of ``bugs.jsonl`` text; a line that is no JSON object with a string ``fp`` and a numeric ``ts`` is
    skipped."""
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("fp"), str) and isinstance(row.get("ts"), (int, float)):
            rows.append(row)
    return rows


def crash_rows(text: str, secrets: list[str]) -> list[dict[str, Any]]:
    """``tui_crash`` rows from crash log text: uncaught exceptions, unhandled rejections and nonzero gateway exits.
    Other lines (spawned, reconnects, kills, the child's own output) are skipped; each summary is scrubbed."""
    rows = []
    for line in text.splitlines():
        m = _CRASH_LINE.match(line)
        if not m:
            continue
        stamp, payload = m.groups()
        scope = _CRASH_SCOPE.match(payload)
        if scope:
            exc_type = scope.group(1)
        elif _CHILD_EXIT.match(payload):
            exc_type = "gateway_exit"
        else:
            continue
        try:
            ts = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            continue
        summary = bugcatch._scrub(payload, secrets)[: bugcatch._SUMMARY_CHARS]
        rows.append(
            {
                "ts": ts,
                "kind": "tui_crash",
                "fp": bugcatch.fingerprint("tui_crash", exc_type, payload, ""),
                "summary": summary,
                "exc_type": exc_type,
                "traceback": "",
                "ctx": {"source": str(CRASH_LOG)},
            }
        )
    return rows


def group(rows: list[dict[str, Any]]) -> list[Group]:
    """One :class:`Group` per fingerprint, newest first. A group is open when one of its rows is newer than its latest
    ``resolved`` row."""
    resolved: dict[str, float] = {}
    by_fp: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda r: r["ts"]):
        if row.get("kind") == RESOLVED:
            resolved[row["fp"]] = max(resolved.get(row["fp"], row["ts"]), row["ts"])
        else:
            by_fp.setdefault(row["fp"], []).append(row)
    out = []
    for fp, members in by_fp.items():
        latest = members[-1]
        suppressed = sum(int(r.get("suppressed") or 0) for r in members)
        out.append(
            Group(
                fp=fp,
                kind=str(latest.get("kind", "")),
                count=len(members),
                first_seen=members[0]["ts"],
                last_seen=latest["ts"],
                summary=str(latest.get("summary", "")),
                suppressed=suppressed,
                open=fp not in resolved or latest["ts"] > resolved[fp],
                latest=latest,
            )
        )
    return sorted(out, key=lambda g: g.last_seen, reverse=True)


def suspected_file(record: dict[str, Any]) -> str:
    """``k3code/<file>:<line>`` of the innermost k3code frame in ``record``'s traceback, else ``unknown``."""
    frames = _TB_FRAME.findall(str(record.get("traceback") or ""))
    if not frames:
        return "unknown"
    path, line = frames[-1]
    return f"{path[path.rindex('k3code/') :]}:{line}"


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_brief(selected: list[Group], *, since_days: float, now: float) -> str:
    """The Markdown brief for ``selected``: the header, then one section per group."""
    parts = [
        HEADER,
        "",
        f"{len(selected)} open bug group(s) seen in the last {since_days:g} day(s), as of {_when(now)}.",
    ]
    for n, g in enumerate(selected, 1):
        ctx = g.latest.get("ctx")
        keys = ", ".join(sorted(str(k) for k in ctx)) if isinstance(ctx, dict) and ctx else "none"
        tb = str(g.latest.get("traceback") or "").strip() or "(no traceback)"
        parts += [
            "",
            f"## {n}. {g.kind}: {g.summary.splitlines()[0] if g.summary else ''}",
            "",
            f"- fp: `{g.fp}`",
            f"- kind: {g.kind}",
            f"- count: {g.count} (suppressed repeats: {g.suppressed})",
            f"- first seen: {_when(g.first_seen)}",
            f"- last seen: {_when(g.last_seen)}",
            f"- suspected file: {suspected_file(g.latest)}",
            f"- ctx keys: {keys}",
            "",
            "Summary:",
            "",
            g.summary,
            "",
            "Traceback (latest record):",
            "",
            "```text",
            tb,
            "```",
        ]
    return "\n".join(parts) + "\n"


def _read_bugs(path: Path) -> str:
    """``bugs.jsonl`` text, read under a shared flock (the writer may be halving it in place)."""
    with path.open("rb") as f:
        if bugcatch.fcntl is not None:
            bugcatch.fcntl.flock(f.fileno(), bugcatch.fcntl.LOCK_SH)
        return f.read().decode("utf-8", "replace")


def rows() -> list[dict[str, Any]]:
    """Every row of ``bugs.jsonl`` plus the ``tui_crash`` rows of the crash log."""
    from k3code.daemon import k3_home

    home = k3_home()
    out: list[dict[str, Any]] = []
    if (home / bugcatch.FILE).exists():
        out += parse_rows(_read_bugs(home / bugcatch.FILE))
    if (home / CRASH_LOG).exists():
        text = (home / CRASH_LOG).read_text(encoding="utf-8", errors="replace")
        out += crash_rows(text, bugcatch._secrets())
    return out


def groups(*, include_resolved: bool = False) -> list[Group]:
    """The bug groups, newest first; resolved ones only when asked for."""
    return [g for g in group(rows()) if include_resolved or g.open]


def show(fp: str) -> dict[str, Any] | None:
    """The latest full record of group ``fp``, resolved or not."""
    for g in group(rows()):
        if g.fp == fp:
            return g.latest
    return None


def resolve(fp: str) -> list[str]:
    """Mark group ``fp`` resolved, or every open group for ``"all"``, by appending a ``resolved`` row through the
    locked writer. Returns the fingerprints marked; an unknown ``fp`` marks nothing."""
    every = group(rows())
    marked = [g.fp for g in every if (g.open if fp == "all" else g.fp == fp)]
    for one in marked:
        bugcatch._append(json.dumps({"kind": RESOLVED, "fp": one, "ts": time.time()}) + "\n")
    return marked


def export(path: Path | None = None, since_days: float = 7) -> Path:
    """Write the brief of the open groups seen in the last ``since_days`` days to ``path`` (default
    ``$K3CODE_HOME/bugs/brief-<ts>.md``), mode 0600, re-scrubbed as a whole. Returns the path."""
    from k3code.daemon import k3_home

    now = time.time()
    selected = [g for g in groups() if g.last_seen >= now - since_days * _DAY]
    brief = bugcatch._scrub(format_brief(selected, since_days=since_days, now=now), bugcatch._secrets())
    if path is None:
        ensure_private_dir(k3_home() / "bugs")
        path = k3_home() / "bugs" / f"brief-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}.md"
    fd = os.open(private_file(path), os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(brief)
    return path
