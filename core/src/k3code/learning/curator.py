# Design ported from hermes-agent agent/curator.py (MIT): idle-time skill curation (usage tracking, stale marking,
# duplicate detection) that proposes rather than deletes. Re-implemented on k3code's skill layout.
"""Skills curator: track usage, mark stale skills, dedup, propose merges."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.learning.fsutil import write_text_atomic
from k3code.learning.ranking import jaccard, tokens
from k3code.paths import home
from k3code.skills import parse_frontmatter

logger = logging.getLogger(__name__)

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
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # temp file + rename: a crash (or a second process) mid-write left a truncated file, which load_usage then read
    # as empty and the next write made every count start over
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


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
            out.append(
                {
                    "name": fm.get("name") or d.name,
                    "dir": d,
                    "desc": fm.get("description", ""),
                    "text": text,
                    "mtime": md.stat().st_mtime,
                }
            )
    return out


def stale_skills(
    skills: list[dict[str, Any]], usage: dict[str, dict[str, Any]], now: float, days: int = 30
) -> list[tuple[dict[str, Any], str]]:
    stale = []
    for s in skills:
        u = usage.get(s["name"], {})
        last = u.get("last_used") or s["mtime"]
        if u.get("failures", 0) >= 2 and u["failures"] >= u.get("uses", 0) / 2:
            stale.append((s, f"failed {u['failures']}× of {u.get('uses', 0)} uses"))
        elif now - last > days * 86400:
            stale.append((s, f"unused for {int((now - last) / 86400)} days"))
    return stale


def find_stale(
    skills: list[dict[str, Any]], usage: dict[str, dict[str, Any]], now: float, days: int = 30
) -> list[tuple[str, str]]:
    return [(s["name"], why) for s, why in stale_skills(skills, usage, now, days)]


def duplicate_skills(skills: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any], float]]:
    # each skill is tokenised once (it was once per pair: 60 skills froze the event loop for 300 ms at a turn's end)
    bags = [tokens(s["name"] + " " + s["desc"] + " " + s["text"][:1500]) for s in skills]
    pairs = []
    for i, a in enumerate(skills):
        for j in range(i + 1, len(skills)):
            sim = jaccard(bags[i], bags[j])
            if sim >= DUP_THRESHOLD:
                pairs.append((a, skills[j], round(sim, 2)))
    return pairs


def find_duplicates(skills: list[dict[str, Any]]) -> list[tuple[str, str, float]]:
    return [(a["name"], b["name"], sim) for a, b, sim in duplicate_skills(skills)]


def curate(
    store: ProposalStore, *, now: float | None = None, stale_days: int = 30, root: Path | None = None
) -> dict[str, Any]:
    """Mark stale skills (``.curator.json``) and propose merges for duplicates. Never deletes.

    A skill is named by its frontmatter ``name``, which need not be its directory's name: the proposals carry the
    directory too, so accepting one archives the skill that was judged, not another with that directory name."""
    now = time.time() if now is None else now
    root = root or skills_dir()
    skills = list_skills(root)
    usage = load_usage()
    stale = stale_skills(skills, usage, now, stale_days)
    marks = {s["name"]: {"state": "stale", "reason": why, "at": now} for s, why in stale}
    if root.is_dir():
        try:
            write_text_atomic(root / ".curator.json", json.dumps(marks, indent=1))
        except OSError as e:  # the marks are a courtesy to /skills: the proposals below do not depend on them
            logger.warning("skills curator: %s not written: %s", root / ".curator.json", e)
    proposals: list[Proposal] = []
    for keep, drop, sim in duplicate_skills(skills):
        kname, dname = keep["name"], drop["name"]
        p = store.add(
            "skill",
            f"Skills '{kname}' and '{dname}' overlap ({int(sim * 100)}%) → merge (archive '{dname}')?",
            f"archive skill {dname}",
            payload={
                "op": "merge",
                "keep": kname,
                "drop": dname,
                "keep_dir": keep["dir"].name,
                "drop_dir": drop["dir"].name,
                "similarity": sim,
            },
            key=dedup_key("skill", f"merge {kname} {dname}"),
        )
        if p:
            proposals.append(p)
    for skill, why in stale:
        name = skill["name"]
        p = store.add(
            "skill",
            f"Skill '{name}' looks stale ({why}) → archive it?",
            f"archive skill {name}",
            payload={"op": "archive", "drop": name, "drop_dir": skill["dir"].name},
            key=dedup_key("skill", f"stale {name}"),
        )
        if p:
            proposals.append(p)
    return {"stale": [(s["name"], why) for s, why in stale], "proposals": proposals}


def _safe_dir_name(name: Any) -> bool:
    """A direct child of the skills root that list_skills would see: no separators, no ``..``, not hidden or ``_``."""
    return (
        isinstance(name, str)
        and bool(name)
        and name == Path(name).name
        and "\\" not in name
        and "\0" not in name
        and not name.startswith((".", "_"))
    )


def _skill_dir(root: Path, name: str, dirname: Any) -> Path | None:
    """The directory of the skill a proposal named: its recorded directory, else (proposals made before they carried
    one named the skill by its frontmatter name) the one skill with that name, never a guess."""
    if dirname is not None:
        return root / dirname if _safe_dir_name(dirname) and (root / dirname).is_dir() else None
    found = [s["dir"] for s in list_skills(root) if s["name"] == name]
    return found[0] if len(found) == 1 else None


def apply(payload: dict[str, Any], root: Path | None = None) -> str:
    root = root or skills_dir()
    op = payload.get("op")
    if op == "save":
        src = Path(payload["draft"])
        if not _safe_dir_name(payload["name"]):
            return f"skill name {payload['name']!r} is not a plain directory name; draft kept at {src}"
        dest = root / payload["name"]
        if dest.exists():
            return f"skill {payload['name']} already exists; draft kept at {src}"
        shutil.move(str(src.parent), str(dest))
        return f"skill saved to {dest}"
    if op in ("merge", "archive"):
        label = payload["drop"]
        src = _skill_dir(root, label, payload.get("drop_dir"))
        if src is None or not src.is_dir():
            return f"skill {label} not found"
        dest = root / "_archive" / src.name
        if dest.exists():
            dest = dest.with_name(f"{src.name}-{int(time.time())}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return f"archived {label} → {dest}"
    return "noted"
