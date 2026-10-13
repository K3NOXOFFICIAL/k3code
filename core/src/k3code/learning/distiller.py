# Design ported from hermes-agent agent/background_review.py (MIT): a cheap-tier pass that turns history into
# durable notes written to a bounded, machine-owned section of a memory file.
"""Preference distiller: decision patterns → ``USER.md`` auto section + ``preferences.json`` (+ mem0)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import ProposalStore, dedup_key
from k3code.learning.decisions import DecisionLog, project_id
from k3code.paths import ensure_private_dir, private_file
from k3code.providers.types import Message
from k3code.redact import scrub_text
from k3code.routing.tiers import TaskKind

HEADING = "## Learned preferences (auto)"
_NOTE = "<!-- managed by k3code; rewritten automatically, edit outside this section -->"
#: Closes the managed block. Only the lines from the heading to this marker are ever rewritten; whatever follows
#: it (``/memory add --user`` appends at the end of the file) is the user's. Files from before the marker ended
#: the block at the next heading, which swallowed those lines (issue #91).
_END = "<!-- /k3code:auto -->"
_HEADING_LINE = re.compile(r"#{1,2} ")
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


#: Decisions older than this do not count towards a learned preference (the log itself keeps a year).
PREFERENCE_WINDOW_DAYS = 90

_DERIVED_KINDS = ("model_switch", "plan", "approval", "interrupt", "scope", "proposal")


def _sessions(rows: list[dict[str, Any]]) -> int:
    """Distinct sessions in ``rows``; a row that names no session counts as its own."""
    return len({r["session"] or f"row:{r['id']}" for r in rows})


def _by(rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any]) -> dict[Any, list[dict[str, Any]]]:
    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return groups


def derive(
    log: DecisionLog, *, min_evidence: int = 3, since: float | None = None
) -> tuple[list[Preference], list[dict[str, Any]]]:
    """Deterministic pattern extraction. Returns (preferences, task_tier suggestions).

    A preference ends up in the user's memory until the next run drops it, so it must rest on a habit, not on a
    long log: only decisions since ``since`` count, nine repeats inside one session are one session of evidence, and
    the sessions must repeat in one project (a stray row in each of three projects is no habit). A pattern that
    qualifies in several projects keeps its strongest evidence; one that two projects judge in opposite ways
    (welcomed in one, disliked in the other) is no trait of the user and is dropped."""
    rows = {kind: log.query(kind, since=since) for kind in _DERIVED_KINDS}
    best: dict[str, Preference] = {}
    tiers: dict[tuple[str, str], dict[str, Any]] = {}
    for project in sorted({r["project"] for rs in rows.values() for r in rs}):
        in_project = {kind: [r for r in rs if r["project"] == project] for kind, rs in rows.items()}
        prefs, suggestions = _derive_in_project(in_project, min_evidence)
        for p in prefs:
            if p.key not in best or p.evidence > best[p.key].evidence:
                best[p.key] = p
        for s in suggestions:
            kept = tiers.get((s["model"], s["task_kind"]))
            if kept is None or s["evidence"] > kept["evidence"]:
                tiers[(s["model"], s["task_kind"])] = s
    for key in [k for k in best if k.startswith("prop+:") and f"prop-:{k[6:]}" in best]:
        del best[key], best[f"prop-:{key[6:]}"]
    prefs = sorted(best.values(), key=lambda p: (-p.confidence, -p.evidence, p.text))
    return prefs, list(tiers.values())


def _derive_in_project(
    rows: dict[str, list[dict[str, Any]]], min_evidence: int
) -> tuple[list[Preference], list[dict[str, Any]]]:
    """The patterns of one project's decisions; evidence is counted in sessions."""
    prefs: list[Preference] = []
    suggestions: list[dict[str, Any]] = []

    def add(key: str, text: str, n: int) -> None:
        prefs.append(Preference(text=text, confidence=_conf(n, min_evidence), evidence=n, key=key))

    # model switches: moving away from model X
    for (model, kind), switches in _by(
        rows["model_switch"], lambda r: (r["detail"].get("from", ""), r["detail"].get("task_kind") or "")
    ).items():
        n = _sessions(switches)
        if n >= min_evidence and model:
            add(f"avoid:{model}:{kind}", f"avoids model {model}" + (f" for {kind} tasks" if kind else ""), n)
            to_tier = _by(
                [r for r in switches if kind and r["detail"].get("to_tier")], lambda r: r["detail"]["to_tier"]
            )
            if to_tier:
                tier, moved = max(to_tier.items(), key=lambda kv: _sessions(kv[1]))
                if _sessions(moved) >= min_evidence:
                    suggestions.append({"model": model, "task_kind": kind, "tier": tier, "evidence": _sessions(moved)})

    # plans
    plans = rows["plan"]
    no_verif = _sessions(
        [r for r in plans if r["choice"] in ("deny", "rejected") and r["detail"].get("has_verification") is False]
    )
    if no_verif >= max(2, min_evidence - 1):
        add("plan:verification", "rejects plans without verification steps", no_verif)
    edited = _sessions([r for r in plans if r["detail"].get("edited")])
    if edited >= min_evidence:
        add("plan:edits", "usually edits plans before approving them", edited)

    # approvals
    tests = [r for r in rows["approval"] if r["choice"] != "deny" and TEST_RE.search(r["subject"])]
    if _sessions(tests) >= min_evidence + 2:
        add("approve:tests", "always wants tests run (approves test commands without friction)", _sessions(tests))
    # interrupts
    for what, interrupts in _by([r for r in rows["interrupt"] if r["subject"]], lambda r: r["subject"]).items():
        if _sessions(interrupts) >= min_evidence:
            add(
                f"interrupt:{what}",
                f"often interrupts `{what}` steps; prefer smaller steps there",
                _sessions(interrupts),
            )
    # scope overrides
    for level, overrides in _by(rows["scope"], lambda r: r["choice"]).items():
        if _sessions(overrides) >= min_evidence:
            add(f"scope:{level}", f"often overrides task scope to '{level}'", _sessions(overrides))
    # proposals by kind
    for kind, proposals in _by(rows["proposal"], lambda r: r["detail"].get("kind") or "?").items():
        decided = [r for r in proposals if r["choice"] in ("accept", "dismiss")]
        n = _sessions(decided)
        if n >= min_evidence:
            rate = sum(1 for r in decided if r["choice"] == "accept") / len(decided)
            if rate >= 0.75:
                add(f"prop+:{kind}", f"welcomes '{kind}' proposals", n)
            elif rate <= 0.25:
                add(f"prop-:{kind}", f"dislikes '{kind}' proposals; propose them rarely", n)
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
                out.append({"prefix": prefix, "reason": scrub_text(text), "evidence": len(reasons)})
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


def _write_user_memory(path: Path, text: str) -> None:
    """USER.md is private state (learned preferences): 0600 in a 0700 directory whatever the umask."""
    ensure_private_dir(path.parent)
    private_file(path).write_text(text, encoding="utf-8")


def add_user_line(path: Path, text: str, heading: str = HEADING) -> None:
    """Add ``- text`` to ``path`` above the auto section (the distiller rewrites that section on every run)."""
    text = scrub_text(text.strip())
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = existing.splitlines(keepends=True)
    at = next((i for i, ln in enumerate(lines) if ln.strip() == heading), None)
    if at is None:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        new = existing + sep + f"- {text}\n"
    else:
        new = "".join(lines[:at]) + f"- {text}\n\n" + "".join(lines[at:])
    _write_user_memory(path, new)


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
    new = {str(i.get("key")): scrub_text(str(i.get("text", "")).strip()) for i in items if isinstance(i, dict)}
    for p in prefs:
        t = new.get(p.key)
        if t and len(t) <= 160 and "\n" not in t:
            p.text = t
    return prefs


def _auto_end(lines: list[str], start: int) -> tuple[int, bool]:
    """Where the auto section that starts at ``lines[start]`` ends: just past its end marker (closed), or, in a file
    from before the marker, at the next heading or the end of the file."""
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == _END:
            return i + 1, True
        if _HEADING_LINE.match(lines[i]):
            return i, False
    return len(lines), False


def write_auto_section(
    path: Path, prefs: list[Preference], heading: str = HEADING, known: Collection[str] = ()
) -> None:
    """Replace the auto section of ``path`` (the heading through the end marker); all other text is preserved byte
    for byte. A section from before the end marker may hold lines the user added after it was written (they cannot be
    told apart from ours): those stay, moved above the heading. ``known`` is the text of the preferences an earlier
    run wrote, which are dropped with the old section."""
    body = "\n".join(f"- {p.text}" for p in prefs) or "- (nothing learned yet)"
    section = f"{heading}\n{_NOTE}\n{body}\n{_END}\n"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == heading), None)
    if start is None:
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        new = text + sep + section
    else:
        end, closed = _auto_end(lines, start)
        tail = "".join(lines[end:])
        if closed:
            new = "".join(lines[:start]) + section + tail
        else:
            ours = {f"- {t}" for t in (*known, *(p.text for p in prefs))} | {"- (nothing learned yet)"}
            mine = [
                ln
                for ln in lines[start + 1 : end]
                if ln.strip() and ln.strip() not in ours and not ln.strip().startswith("<!-- managed by k3code")
            ]
            kept = "".join(ln if ln.endswith("\n") else ln + "\n" for ln in mine)
            new = "".join(lines[:start]) + (kept + "\n" if kept else "") + section + ("\n" + tail if tail else "")
    _write_user_memory(path, new)


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
        if ln.strip() == _END or _HEADING_LINE.match(ln):
            break
        if ln.startswith("- ") and "(nothing learned" not in ln:
            out.append(ln[2:].strip())
    return out


def write_preferences_json(home: Path, prefs: list[Preference]) -> Path:
    path = home / "learning" / "preferences.json"
    ensure_private_dir(path.parent)
    private_file(path)
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
    try:
        prefs = [Preference(v["text"], float(v["confidence"]), int(v["evidence"]), k) for k, v in data.items()]
    except (AttributeError, KeyError, TypeError, ValueError):  # valid JSON of the wrong shape: nothing known
        return []
    prefs.sort(key=lambda p: (-p.confidence, -p.evidence))
    return prefs[:top]


#: Keys of what store_mem0 already posted (newest last), so the daily distill does not post every preference again.
MEM0_POSTED = "mem0_posted.json"
MEM0_POSTED_MAX = 2000


def project_scope(cwd: str | Path, project: str = "") -> dict[str, str]:
    """The mem0 scope of a fact that is only true of one project: its stable id and a short repo name."""
    pid = project or project_id(cwd)
    repo = re.split(r"[/:]", pid)[-1] if pid.startswith("git:") else Path(cwd).resolve().name
    return {"project": pid, "repo": repo or "project", "scope": "project"}


def _posted_key(m: Any, text: str, scope: dict[str, str] | None = None) -> str:
    """Same text to the same mem0 target = same key; another url, agent, user or project posts again."""
    where = f"\0{scope['project']}" if scope else ""
    return hashlib.sha256(f"{m.url}\0{m.agent_id}\0{m.user_id}{where}\0{text}".encode()).hexdigest()[:24]


def _load_posted(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(k) for k in data] if isinstance(data, list) else []


def _write_private_json(path: Path, data: Any) -> None:
    ensure_private_dir(path.parent)
    tmp = private_file(path.with_name(f"{path.name}.{os.getpid()}.tmp"))
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _save_posted(path: Path, keys: list[str]) -> None:
    _write_private_json(path, keys[-MEM0_POSTED_MAX:])


#: What mem0 did not take (it was down, or the pass stopped before the item): the next pass posts it first. A review
#: fact is derived once, so without this a failed post lost it (a preference is re-derived by the daily distill).
MEM0_OUTBOX = "mem0_outbox.json"
MEM0_OUTBOX_MAX = 200
#: One store_mem0 pass at a time: two passes would read the same ledger and outbox and overwrite each other's writes.
_MEM0_LOCK = threading.Lock()


def _load_outbox(path: Path) -> list[tuple[Preference, dict[str, str] | None]]:
    """What the last pass could not send: each preference with the project scope it was derived under, if any."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            (
                Preference(str(e["text"]), float(e["confidence"]), int(e["evidence"])),
                {str(k): str(v) for k, v in e["scope"].items()} if isinstance(e.get("scope"), dict) else None,
            )
            for e in data
        ]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return []


def _save_outbox(path: Path, items: list[tuple[Preference, dict[str, str] | None]]) -> None:
    if not items:
        path.unlink(missing_ok=True)
        return
    _write_private_json(
        path,
        [
            {"text": p.text, "confidence": p.confidence, "evidence": p.evidence, **({"scope": sc} if sc else {})}
            for p, sc in items[-MEM0_OUTBOX_MAX:]
        ],
    )


def _refused(exc: Exception) -> bool:
    """mem0 answered and refused this one body (400, 413, 422): retrying cannot work and the next item may be fine.
    Anything else (no connection, a timeout, 401/403, 429, 5xx) means mem0 is not usable for the rest of the pass."""
    return getattr(getattr(exc, "response", None), "status_code", 0) in (400, 413, 422)


def store_mem0(
    config: Any,
    prefs: list[Preference],
    post: Callable[[str, dict[str, Any], dict[str, str]], None] | None = None,
    posted: Path | None = None,
    outbox: Path | None = None,
    scope: dict[str, str] | None = None,
) -> int:
    """Store each preference as a mem0 memory when mem0 is configured. The key comes from the env only.

    ``posted`` is a JSON file of what was already sent: a preference posted before is skipped (the daily distill
    re-derives every preference and posted each one again, every day). ``outbox`` is a JSON file of what could not
    be sent (each with its own scope): it is posted first on the next pass. ``scope`` (see :func:`project_scope`)
    marks a project fact: it names the project in the text and metadata, so another repo's recall does not read
    it as a trait of the user. The first failed post ends the pass (a host that does not answer would cost the connect
    timeout once, not once per item) and every item not yet sent goes to the outbox.
    Returns how many were posted now."""
    m = getattr(config, "mem0", None)
    if m is None or not m.url:
        return 0
    with _MEM0_LOCK:
        done = _load_posted(posted) if posted is not None else []
        seen = set(done)
        queued = _load_outbox(outbox) if outbox is not None else []
        headers = {"Content-Type": "application/json"}
        if m.api_key_env and os.environ.get(m.api_key_env):
            headers["Authorization"] = f"Token {os.environ[m.api_key_env]}"
        if post is None:
            import httpx

            def post(url: str, body: dict[str, Any], hdrs: dict[str, str]) -> None:  # noqa: F811
                httpx.post(url, json=body, headers=hdrs, timeout=httpx.Timeout(10, connect=2)).raise_for_status()

        n = 0
        unsent: list[tuple[Preference, dict[str, str] | None]] = []
        down = False
        for p, sc in [*queued, *((p, scope) for p in prefs)]:
            key = _posted_key(m, p.text, sc)
            if key in seen:
                continue
            seen.add(key)  # one try per text and pass, whether it came from the outbox or from prefs
            if down:
                unsent.append((p, sc))
                continue
            label = f"Project fact ({sc['repo']}, learned by k3code)" if sc else "User preference (learned by k3code)"
            body = {
                "messages": [{"role": "user", "content": f"{label}: {p.text}"}],
                **m.identifiers(),
                "metadata": {
                    "confidence": p.confidence,
                    "evidence": p.evidence,
                    "source": "k3code-distiller",
                    **(sc or {}),
                },
            }
            try:
                post(m.url.rstrip("/") + "/memories", body, headers)
            except Exception as e:  # noqa: BLE001 - mem0 is best effort; what it did not take waits in the outbox
                if not _refused(e):
                    down = True
                    unsent.append((p, sc))
                continue
            n += 1
            done.append(key)
        if posted is not None and n:
            _save_posted(posted, done)
        if outbox is not None and (unsent or queued):
            _save_outbox(outbox, unsent)
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
    window_days: float = PREFERENCE_WINDOW_DAYS,
    session_id: str = "",
) -> list[Preference]:
    prefs, suggestions = derive(log, min_evidence=min_evidence, since=log.clock() - window_days * 86400)
    prefs = await polish(caller, prefs, session_id=session_id)
    last = [p.text for p in load_preferences(home, top=1_000_000)]  # what the previous run wrote, to migrate USER.md
    write_auto_section(user_md, prefs, known=last)
    write_preferences_json(home, prefs)
    # blocking HTTP (2 s to connect, 10 s per request): never on the event loop that serves every session
    await asyncio.to_thread(
        store_mem0, config, prefs, mem0_post, home / "learning" / MEM0_POSTED, home / "learning" / MEM0_OUTBOX
    )
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
