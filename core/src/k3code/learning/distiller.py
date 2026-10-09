# Design ported from hermes-agent agent/background_review.py (MIT): a cheap-tier pass that turns history into
# durable notes written to a bounded, machine-owned section of a memory file.
"""Preference distiller: decision patterns → ``USER.md`` auto section + ``preferences.json`` (+ mem0)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import ProposalStore, dedup_key
from k3code.learning.decisions import DecisionLog, scrub
from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

HEADING = "## Learned preferences (auto)"
_NOTE = "<!-- managed by k3code; rewritten automatically, edit outside this section -->"
TEST_RE = re.compile(
    r"\b(pytest|npm test|npm run test|yarn test|pnpm test|cargo test|go test|make test|uv run pytest)\b"
)


@dataclass
class Preference:
    text: str
    confidence: float
    evidence: int
    key: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _conf(n: int, base: int = 3) -> float:
    return round(min(0.95, 0.5 + 0.1 * (n - base + 1)), 2) if n >= base else round(0.3 + 0.05 * n, 2)


def derive(log: DecisionLog, *, min_evidence: int = 3) -> tuple[list[Preference], list[dict[str, Any]]]:
    """Deterministic pattern extraction. Returns (preferences, task_tier suggestions)."""
    prefs: list[Preference] = []

    def add(key: str, text: str, n: int) -> None:
        prefs.append(Preference(text=text, confidence=_conf(n, min_evidence), evidence=n, key=key))

    # model switches: moving away from model X
    away: Counter[tuple[str, str]] = Counter()
    tiers: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for r in log.query("model_switch"):
        d = r["detail"]
        kind = d.get("task_kind") or ""
        away[(d.get("from", ""), kind)] += 1
        if kind and d.get("to_tier"):
            tiers[(d.get("from", ""), kind)][d["to_tier"]] += 1
    suggestions: list[dict[str, Any]] = []
    for (model, kind), n in away.items():
        if n >= min_evidence and model:
            add(f"avoid:{model}:{kind}", f"avoids model {model}" + (f" for {kind} tasks" if kind else ""), n)
            if kind and tiers[(model, kind)]:
                tier, tn = tiers[(model, kind)].most_common(1)[0]
                if tn >= min_evidence:
                    suggestions.append({"model": model, "task_kind": kind, "tier": tier, "evidence": tn})

    # plans
    plans = log.query("plan")
    no_verif = sum(
        1 for r in plans if r["choice"] in ("deny", "rejected") and r["detail"].get("has_verification") is False
    )
    if no_verif >= max(2, min_evidence - 1):
        add("plan:verification", "rejects plans without verification steps", no_verif)
    edited = sum(1 for r in plans if r["detail"].get("edited"))
    if edited >= min_evidence:
        add("plan:edits", "usually edits plans before approving them", edited)

    # approvals
    tests = [r for r in log.query("approval") if r["choice"] != "deny" and TEST_RE.search(r["subject"])]
    if len(tests) >= min_evidence + 2:
        add("approve:tests", "always wants tests run (approves test commands without friction)", len(tests))
    # interrupts
    ints = Counter(r["subject"] for r in log.query("interrupt") if r["subject"])
    for what, n in ints.items():
        if n >= min_evidence:
            add(f"interrupt:{what}", f"often interrupts `{what}` steps; prefer smaller steps there", n)
    # scope overrides
    sc = Counter(r["choice"] for r in log.query("scope"))
    for level, n in sc.items():
        if n >= min_evidence:
            add(f"scope:{level}", f"often overrides task scope to '{level}'", n)
    # undo
    undo = log.count("undo")
    if undo >= min_evidence:
        add("undo", "frequently rolls back changes; prefer small, reviewable edits", undo)
    # proposals by kind
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    for r in log.query("proposal"):
        by_kind[r["detail"].get("kind") or "?"][r["choice"]] += 1
    for kind, c in by_kind.items():
        tot = c["accept"] + c["dismiss"]
        if tot >= min_evidence:
            rate = c["accept"] / tot
            if rate >= 0.75:
                add(f"prop+:{kind}", f"welcomes '{kind}' proposals", tot)
            elif rate <= 0.25:
                add(f"prop-:{kind}", f"dislikes '{kind}' proposals; propose them rarely", tot)
    prefs.sort(key=lambda p: (-p.confidence, -p.evidence, p.text))
    return prefs, suggestions


def _command_prefix(row: dict[str, Any]) -> str:
    """The program a denied command runs (``npm`` of ``FOO=1 npm install x``); the pattern when no command was kept."""
    words = str(row["detail"].get("command") or row["subject"] or "").split()
    while words and "=" in words[0] and words[0].split("=", 1)[0].isidentifier():
        words.pop(0)
    return words[0] if words else ""


def _reason_words(text: str) -> set[str]:
    return set(re.sub(r"\W+", " ", text.lower()).split())


def _similar(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= 0.5


def denial_preferences(log: DecisionLog, *, min_evidence: int = 2) -> list[dict[str, Any]]:
    """Denials of one command prefix that came with similar reasons ("use pnpm, not npm" twice), as preference
    candidates: ``{"prefix", "reason", "evidence"}``. A reason told once reaches the model for that turn only."""
    groups: dict[tuple[str, str], list[list[Any]]] = defaultdict(list)  # (tool, prefix) -> [[words, reasons], ...]
    for r in log.query("approval"):
        reason = str(r["detail"].get("reason") or "").strip()
        if r["choice"] != "deny" or not reason:
            continue
        tool = str(r["detail"].get("tool") or "")
        prefix = _command_prefix(r) if tool == "bash" else tool
        if not prefix:
            continue
        words = _reason_words(reason)
        clusters = groups[(tool, prefix)]
        for c in clusters:
            if _similar(c[0], words):
                c[1].append(reason)
                break
        else:
            clusters.append([words, [reason]])
    out = []
    for (_tool, prefix), clusters in groups.items():
        for _words, reasons in clusters:
            if len(reasons) >= min_evidence:
                text = Counter(reasons).most_common(1)[0][0]  # the most common wording, the first on a tie
                out.append({"prefix": prefix, "reason": scrub(text), "evidence": len(reasons)})
    return out


def propose_denial_preferences(log: DecisionLog, proposals: ProposalStore) -> list[Any]:
    made = []
    for c in denial_preferences(log):
        line = f"for `{c['prefix']}` commands: {c['reason']}"
        p = proposals.add(
            "preference",
            f'You denied `{c["prefix"]}` {c["evidence"]}× saying "{c["reason"]}" → remember: {line}?',
            "remember this preference",
            payload={"text": line},
            key=dedup_key("preference", f"deny {c['prefix']} {c['reason']}"),
        )
        if p is not None:
            made.append(p)
    return made


def add_user_line(path: Path, text: str, heading: str = HEADING) -> None:
    """Add ``- text`` to ``path`` above the auto section (the distiller rewrites that section, and everything below
    its heading up to the next one, on every run)."""
    text = scrub(text.strip())
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = existing.splitlines(keepends=True)
    at = next((i for i, ln in enumerate(lines) if ln.strip() == heading), None)
    if at is None:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        new = existing + sep + f"- {text}\n"
    else:
        new = "".join(lines[:at]) + f"- {text}\n\n" + "".join(lines[at:])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")


DISTILL_SYSTEM = (
    "You rewrite observed user-behaviour facts as short, durable preference lines for a coding agent's memory. "
    'Reply with ONE JSON array of {"key": str, "text": str}. Keep each text under 100 chars, imperative or '
    "descriptive, one idea each. Do not add facts that are not in the input. Never include secrets or file contents."
)


async def polish(caller: Any, prefs: list[Preference], *, session_id: str = "") -> list[Preference]:
    """Optional cheap-tier rewording; any failure keeps the deterministic text."""
    if caller is None or not prefs:
        return prefs
    payload = json.dumps([{"key": p.key, "text": p.text, "evidence": p.evidence} for p in prefs])
    try:
        res = await caller.complete(
            TaskKind.CLASSIFICATION,
            [Message(role="system", content=DISTILL_SYSTEM), Message(role="user", content=payload)],
            session_id=session_id,
            max_tokens=700,
            timeout=30,
        )
        m = re.search(r"\[.*\]", res.text or "", re.S)
        items = json.loads(m.group(0)) if m else []
    except Exception:  # noqa: BLE001
        return prefs
    new = {str(i.get("key")): scrub(str(i.get("text", "")).strip()) for i in items if isinstance(i, dict)}
    for p in prefs:
        t = new.get(p.key)
        if t and len(t) <= 160 and "\n" not in t:
            p.text = t
    return prefs


def write_auto_section(path: Path, prefs: list[Preference], heading: str = HEADING) -> None:
    """Replace the auto section of ``path``; all other text is preserved byte for byte."""
    body = "\n".join(f"- {p.text}" for p in prefs) or "- (nothing learned yet)"
    section = f"{heading}\n{_NOTE}\n{body}\n"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == heading), None)
    if start is None:
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        new = text + sep + section
    else:
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"#{1,2} ", lines[i])), len(lines))
        tail = "".join(lines[end:])
        new = "".join(lines[:start]) + section + ("\n" + tail if tail else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")


def read_auto_section(path: Path, heading: str = HEADING) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        i = next(i for i, ln in enumerate(lines) if ln.strip() == heading)
    except StopIteration:
        return []
    out = []
    for ln in lines[i + 1 :]:
        if re.match(r"#{1,2} ", ln):
            break
        if ln.startswith("- ") and "(nothing learned" not in ln:
            out.append(ln[2:].strip())
    return out


def write_preferences_json(home: Path, prefs: list[Preference]) -> Path:
    path = home / "learning" / "preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {p.key or p.text: {"text": p.text, "confidence": p.confidence, "evidence": p.evidence} for p in prefs},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def load_preferences(home: Path, top: int = 5) -> list[Preference]:
    path = home / "learning" / "preferences.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    prefs = [Preference(v["text"], float(v["confidence"]), int(v["evidence"]), k) for k, v in data.items()]
    prefs.sort(key=lambda p: (-p.confidence, -p.evidence))
    return prefs[:top]


#: Keys of what store_mem0 already posted (newest last), so the daily distill does not post every preference again.
MEM0_POSTED = "mem0_posted.json"
MEM0_POSTED_MAX = 2000


def _posted_key(m: Any, text: str) -> str:
    """Same text to the same mem0 target = same key; another url, agent or user posts again."""
    return hashlib.sha256(f"{m.url}\0{m.agent_id}\0{m.user_id}\0{text}".encode()).hexdigest()[:24]


def _load_posted(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(k) for k in data] if isinstance(data, list) else []


def _save_posted(path: Path, keys: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(keys[-MEM0_POSTED_MAX:]), encoding="utf-8")
    os.replace(tmp, path)


def store_mem0(
    config: Any,
    prefs: list[Preference],
    post: Callable[[str, dict[str, Any], dict[str, str]], None] | None = None,
    posted: Path | None = None,
) -> int:
    """Store each preference as a mem0 memory when mem0 is configured. The key comes from the env only.

    ``posted`` is a JSON file of what was already sent: a preference posted before is skipped (the daily distill
    re-derives every preference and posted each one again, every day). Returns how many were posted now."""
    m = getattr(config, "mem0", None)
    if m is None or not m.url:
        return 0
    done = _load_posted(posted) if posted is not None else []
    seen = set(done)
    headers = {"Content-Type": "application/json"}
    if m.api_key_env and os.environ.get(m.api_key_env):
        headers["Authorization"] = f"Token {os.environ[m.api_key_env]}"
    if post is None:
        import httpx

        def post(url: str, body: dict[str, Any], hdrs: dict[str, str]) -> None:  # noqa: F811
            httpx.post(url, json=body, headers=hdrs, timeout=10).raise_for_status()

    n = 0
    for p in prefs:
        key = _posted_key(m, p.text)
        if key in seen:
            continue
        body = {
            "messages": [{"role": "user", "content": f"User preference (learned by k3code): {p.text}"}],
            "agent_id": m.agent_id,
            **({"user_id": m.user_id} if m.user_id else {}),
            "metadata": {"confidence": p.confidence, "evidence": p.evidence, "source": "k3code-distiller"},
        }
        try:
            post(m.url.rstrip("/") + "/memories", body, headers)
            n += 1
        except Exception:  # noqa: BLE001 - mem0 is best effort (a failed one is tried again next time)
            continue
        seen.add(key)
        done.append(key)
    if posted is not None and n:
        _save_posted(posted, done)
    return n


async def distill(
    log: DecisionLog,
    *,
    home: Path,
    user_md: Path,
    config: Any = None,
    caller: Any = None,
    proposals: ProposalStore | None = None,
    mem0_post: Any = None,
    min_evidence: int = 3,
    session_id: str = "",
) -> list[Preference]:
    prefs, suggestions = derive(log, min_evidence=min_evidence)
    prefs = await polish(caller, prefs, session_id=session_id)
    write_auto_section(user_md, prefs)
    write_preferences_json(home, prefs)
    # blocking HTTP (up to 10 s per preference): never on the event loop that serves every session
    await asyncio.to_thread(store_mem0, config, prefs, mem0_post, home / "learning" / MEM0_POSTED)
    if proposals is not None:
        propose_denial_preferences(log, proposals)
        for s in suggestions:
            text = (
                f"You keep moving away from {s['model']} for {s['task_kind']} tasks ({s['evidence']}×) → "
                f"route {s['task_kind']} to the '{s['tier']}' tier?"
            )
            proposals.add(
                "optimizer",
                text,
                "update task_tiers",
                payload={"task_tiers": {s["task_kind"]: s["tier"]}},
                key=dedup_key("optimizer", f"task_tiers {s['task_kind']} {s['tier']}"),
            )
    return prefs
