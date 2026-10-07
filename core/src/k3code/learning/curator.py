# Design ported from hermes-agent agent/curator.py (MIT): idle-time skill curation (usage tracking, stale marking,
# duplicate detection) that proposes rather than deletes. Re-implemented on k3code's skill layout.
"""Skills curator: track usage, mark stale skills, dedup, propose merges."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.learning.ranking import jaccard, tokens
from k3code.paths import home
from k3code.skills import parse_frontmatter

DUP_THRESHOLD = 0.7


def skills_dir() -> Path:
    return home() / "skills"


def _usage_path() -> Path:
    return skills_dir() / ".usage.json"


def load_usage() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_use(name: str, *, ok: bool = True, clock: Callable[[], float] = time.time) -> None:
    data = load_usage()
    e = data.setdefault(name, {"uses": 0, "failures": 0, "last_used": 0.0})
    e["uses"] += 1
    e["failures"] += 0 if ok else 1
    e["last_used"] = clock()
    _usage_path().parent.mkdir(parents=True, exist_ok=True)
    _usage_path().write_text(json.dumps(data, indent=1), encoding="utf-8")


def list_skills(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or skills_dir()
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        md = d / "SKILL.md"
        if d.is_dir() and not d.name.startswith((".", "_")) and md.is_file():
            text = md.read_text(encoding="utf-8", errors="replace")
            fm = parse_frontmatter(text[:4000])
            out.append({"name": fm.get("name") or d.name, "dir": d, "desc": fm.get("description", ""), "text": text,
                        "mtime": md.stat().st_mtime})
    return out


def find_stale(skills: list[dict[str, Any]], usage: dict[str, dict[str, Any]], now: float, days: int = 30) -> list[
        tuple[str, str]]:
    stale = []
    for s in skills:
        u = usage.get(s["name"], {})
        last = u.get("last_used") or s["mtime"]
        if u.get("failures", 0) >= 2 and u["failures"] >= u.get("uses", 0) / 2:
            stale.append((s["name"], f"failed {u['failures']}× of {u.get('uses', 0)} uses"))
        elif now - last > days * 86400:
            stale.append((s["name"], f"unused for {int((now - last) / 86400)} days"))
    return stale


def find_duplicates(skills: list[dict[str, Any]]) -> list[tuple[str, str, float]]:
    pairs = []
    for i, a in enumerate(skills):
        for b in skills[i + 1:]:
            sim = jaccard(tokens(a["name"] + " " + a["desc"] + " " + a["text"][:1500]),
                          tokens(b["name"] + " " + b["desc"] + " " + b["text"][:1500]))
            if sim >= DUP_THRESHOLD:
                pairs.append((a["name"], b["name"], round(sim, 2)))
    return pairs


def curate(store: ProposalStore, *, now: float | None = None, stale_days: int = 30, root: Path | None = None
           ) -> dict[str, Any]:
    """Mark stale skills (``.curator.json``) and propose merges for duplicates. Never deletes."""
    now = time.time() if now is None else now
    root = root or skills_dir()
    skills = list_skills(root)
    usage = load_usage()
    stale = find_stale(skills, usage, now, stale_days)
    marks = {name: {"state": "stale", "reason": why, "at": now} for name, why in stale}
    if root.is_dir():
        (root / ".curator.json").write_text(json.dumps(marks, indent=1), encoding="utf-8")
    proposals: list[Proposal] = []
    for keep, drop, sim in find_duplicates(skills):
        p = store.add("skill", f"Skills '{keep}' and '{drop}' overlap ({int(sim * 100)}%) → merge (archive '{drop}')?",
                      f"archive skill {drop}", payload={"op": "merge", "keep": keep, "drop": drop, "similarity": sim},
                      key=dedup_key("skill", f"merge {keep} {drop}"))
        if p:
            proposals.append(p)
    for name, why in stale:
        p = store.add("skill", f"Skill '{name}' looks stale ({why}) → archive it?", f"archive skill {name}",
                      payload={"op": "archive", "drop": name}, key=dedup_key("skill", f"stale {name}"))
        if p:
            proposals.append(p)
    return {"stale": stale, "proposals": proposals}


def apply(payload: dict[str, Any], root: Path | None = None) -> str:
    root = root or skills_dir()
    op = payload.get("op")
    if op == "save":
        src = Path(payload["draft"])
        dest = root / payload["name"]
        if dest.exists():
            return f"skill {payload['name']} already exists; draft kept at {src}"
        shutil.move(str(src.parent), str(dest))
        return f"skill saved to {dest}"
    if op in ("merge", "archive"):
        src = root / payload["drop"]
        if not src.is_dir():
            return f"skill {payload['drop']} not found"
        dest = root / "_archive" / payload["drop"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return f"archived {payload['drop']} → {dest}"
    return "noted"
