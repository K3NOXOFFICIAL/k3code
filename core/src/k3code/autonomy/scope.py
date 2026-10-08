"""Scope gate: classify a new task (trivial … huge) before work starts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

SCOPES = ("trivial", "small", "medium", "large", "huge")
RISKS = ("low", "med", "high")

#: Actions that always force a plan and ``risk=high``.
DANGER_RE = re.compile(
    r"\b(delete|deleting|rm\s+-\w+|drop\s+(table|database|schema)|truncate|wipe|purge|migrat\w+|deploy\w*"
    r"|credentials?|secrets?|passwords?|api[ _-]?keys?|force[- ]?push|push\s+(-f|--force)|reset\s+--hard"
    r"|production)\b",
    re.IGNORECASE,
)

CLASSIFIER_SYSTEM = """\
You classify a coding task by the amount of work it needs, before any work starts.
Reply with ONE JSON object and nothing else (no prose, no code fence):
{"scope": "trivial|small|medium|large|huge", "needs_plan": bool, "risk": "low|med|high",
 "parallelizable": bool, "suggested_subtasks": [str], "reason": str}

Levels. Judge by the work the request literally implies, not by how important it sounds:
- trivial: no design decisions. An answer-only question or explanation, or ONE located edit (a typo, a literal
  value, a version/year/default, a rename on a named line). Under ~5 minutes, nothing to test.
- small: ONE file or function. New or changed logic, a flag, a bug fix, a single unit test, a script. Tests for that
  one change are included. Needs a little reading but no plan.
- medium: a feature slice across roughly 2-8 files: e.g. handler + router + tests + docs, a shared module extracted
  from a few callers, one package-wide mechanical change, UI component + styles + persistence.
- large: a whole feature or one subsystem, roughly 9-60 files or several layers (DB schema, backend, UI, tests): new
  auth/sync/plugin/i18n machinery, a mechanical change across a whole layer or a stated count of 10+ files, a
  complete test suite for one subsystem.
- huge: the entire repo or a whole product: build an application from scratch, change the language or framework of
  the whole codebase, rewrite into a different architecture, split into several deployables, a new engine.

Rules:
- Count scope words: "everywhere", "all", "every", "whole", "throughout" and stated file counts set the level by
  the number of files they imply. A named single file, line or function keeps it at trivial/small.
- Tie-break between neighbours: choose the lower level if the request names one place and no extra wiring; choose the
  higher level if it also needs tests, docs, a migration, UI or several layers. Do not add a level out of caution.
- Never pick huge for one subsystem, and never pick small for work that spans several packages.
- If the likely actions delete data, migrate, deploy, touch credentials or force-push, set needs_plan=true and
  risk=high. Set parallelizable=true only when the work splits into independent parts (usually large/huge).

Examples:
Task: "Change the log level default in app.ini from INFO to WARN" -> {"scope":"trivial","needs_plan":false,"risk":"low","parallelizable":false,"suggested_subtasks":[],"reason":"one literal value"}
Task: "Make the slugify() helper in text.js strip emoji, and add a test" -> {"scope":"small","needs_plan":false,"risk":"low","parallelizable":false,"suggested_subtasks":[],"reason":"one function plus its test"}
Task: "Add rate limiting to the public API: middleware, config option, per-route overrides, tests and docs" -> {"scope":"medium","needs_plan":true,"risk":"low","parallelizable":false,"suggested_subtasks":["middleware","config","tests+docs"],"reason":"one feature across ~6 files"}
Task: "Add a notifications system: DB tables, queue worker, email and push senders, preferences UI, tests" -> {"scope":"large","needs_plan":true,"risk":"med","parallelizable":true,"suggested_subtasks":["schema","worker","senders","UI"],"reason":"new subsystem across layers"}
Task: "Rebuild the whole product as a native desktop app with its own sync backend" -> {"scope":"huge","needs_plan":true,"risk":"med","parallelizable":true,"suggested_subtasks":["architecture","backend","client","packaging"],"reason":"whole new product"}
"""  # noqa: E501 - example lines are one JSON object each


@dataclass
class ScopeVerdict:
    scope: str = "small"
    needs_plan: bool = False
    risk: str = "low"
    parallelizable: bool = False
    suggested_subtasks: list[str] = field(default_factory=list)
    reason: str = ""
    #: classifier | override | fallback
    source: str = "classifier"
    #: large/huge: the M4b fan-out executor could take this; for now it runs sequentially
    fanout_candidate: bool = False

    @property
    def wants_plan(self) -> bool:
        """medium and above, or an explicit/forced plan."""
        return self.needs_plan or SCOPES.index(self.scope) >= SCOPES.index("medium")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode()).hexdigest()[:16]


_SCOPE_ALIASES = {"tiny": "trivial", "minor": "small", "moderate": "medium", "big": "large",
                  "xl": "huge", "massive": "huge", "enormous": "huge", "project": "huge"}
_SCOPE_LINE_RE = re.compile(r"\bscope\W{0,6}(trivial|small|medium|large|huge)\b", re.IGNORECASE)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1")
    return bool(value)


def _norm_level(value: Any) -> str | None:
    word = re.sub(r"[^a-z]", "", str(value).lower())
    word = _SCOPE_ALIASES.get(word, word)
    return word if word in SCOPES else None


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Every top-level JSON object in ``text`` (fences/prose around them are skipped)."""
    dec, out, i = json.JSONDecoder(), [], 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            obj, end = None, i + 1
            tail = text[i : text.rfind("}") + 1]  # one cheap repair: trailing commas before } or ]
            try:
                fixed = json.loads(re.sub(r",\s*([}\]])", r"\1", tail))
                if isinstance(fixed, dict):
                    out.append(fixed)
                    break
            except ValueError:
                pass
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def parse_reply(text: str) -> tuple[ScopeVerdict | None, str]:
    """Parse a classifier reply into ``(verdict, why)``; ``verdict`` is None only when nothing usable was found.

    Tolerates code fences, prose around (or before) the JSON, several objects, wrong-case keys and labels, string
    booleans and missing optional fields. A reply without a JSON ``scope`` still yields a verdict if it says
    ``scope: <level>`` in prose. ``why`` says what was repaired or why it failed.
    """
    if not (text or "").strip():
        return None, "empty reply"
    notes: list[str] = []
    for obj in _json_objects(text):
        data = {str(k).strip().lower(): v for k, v in obj.items()}
        if "scope" not in data:
            continue
        scope = _norm_level(data["scope"])
        if scope is None:
            notes.append(f"unknown scope {str(data['scope'])[:20]!r}")
            continue
        risk = str(data.get("risk", "low")).strip().lower()
        subtasks = data.get("suggested_subtasks") or []
        missing = [k for k in ("needs_plan", "risk", "parallelizable") if k not in data]
        if scope != str(data["scope"]):
            notes.append("scope label normalised")
        if missing:
            notes.append("missing " + ",".join(missing))
        return ScopeVerdict(
            scope=scope,
            needs_plan=_as_bool(data.get("needs_plan", False)),
            risk=risk if risk in RISKS else "med",
            parallelizable=_as_bool(data.get("parallelizable", False)),
            suggested_subtasks=[str(s) for s in subtasks if s] if isinstance(subtasks, list) else [],
            reason=str(data.get("reason", "")),
        ), "; ".join(notes)
    m = _SCOPE_LINE_RE.search(text)
    if m:
        return ScopeVerdict(scope=m.group(1).lower(), reason="recovered from prose"), "no JSON; scope read from prose"
    if notes:
        return None, "; ".join(notes)
    return None, "no JSON object with a scope" if "{" in text else "no JSON object in reply"


def parse_verdict(text: str) -> ScopeVerdict | None:
    """The classifier's verdict, or None when the reply is unusable (see :func:`parse_reply` for why)."""
    return parse_reply(text)[0]


def fallback_verdict(why: str) -> ScopeVerdict:
    """Safe default when the classifier gave nothing usable: ``small``, recorded as a fallback with the reason."""
    return ScopeVerdict(scope="small", source="fallback", reason=f"classifier unusable ({why}); heuristics only")


def apply_floor(verdict: ScopeVerdict, prompt: str) -> ScopeVerdict:
    """Safety floor: danger classes in the prompt or the predicted actions force a high-risk plan."""
    haystack = " ".join([prompt, verdict.reason, *verdict.suggested_subtasks])
    hit = DANGER_RE.search(haystack)
    if hit:
        verdict.needs_plan = True
        verdict.risk = "high"
        if f"danger: {hit.group(0).lower()}" not in verdict.reason:
            verdict.reason = (verdict.reason + f" [danger: {hit.group(0).lower()}]").strip()
    if verdict.scope in ("large", "huge"):
        verdict.fanout_candidate = True
    return verdict


def from_override(level: str, prompt: str) -> ScopeVerdict:
    """``/scope <level>``: skip the classifier; the danger floor still applies."""
    v = ScopeVerdict(scope=level, needs_plan=level in ("large", "huge"), source="override",
                     reason=f"/scope {level} override", parallelizable=level in ("large", "huge"))
    return apply_floor(v, prompt)


def repo_summary(cwd: Path, *, max_files: int = 5000) -> str:
    """File count, top languages and git status, cheap enough to run per task."""
    exts: dict[str, int] = {}
    count = 0
    skip = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".mypy_cache", ".ruff_cache"}
    for _root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            count += 1
            ext = Path(f).suffix.lower()
            if ext:
                exts[ext] = exts.get(ext, 0) + 1
        if count >= max_files:
            break
    top = ", ".join(f"{e}×{n}" for e, n in sorted(exts.items(), key=lambda kv: -kv[1])[:5]) or "none"
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=5,
                             stdin=subprocess.DEVNULL)
        git = f"{len(out.stdout.splitlines())} changed files" if out.returncode == 0 else "not a git repo"
    except (OSError, subprocess.SubprocessError):
        git = "git unavailable"
    more = "+" if count >= max_files else ""
    return f"{count}{more} files; languages: {top}; git: {git}"


def classifier_messages(prompt: str, summary: str, recent: str) -> list[Message]:
    body = f"Task:\n{prompt}\n\nRepo: {summary}\n"
    if recent:
        body += f"\nRecent context:\n{recent}\n"
    return [Message(role="system", content=CLASSIFIER_SYSTEM), Message(role="user", content=body)]


async def classify(caller: Any, prompt: str, cwd: Path, recent: str = "", session_id: str = "") -> ScopeVerdict:
    """Classify with the ``classification`` tier; classifier failure falls back to a heuristic ``small``."""
    try:
        res = await caller.complete(
            TaskKind.CLASSIFICATION,
            classifier_messages(prompt, repo_summary(cwd), recent),
            session_id=session_id,
            max_tokens=400,
            timeout=20,
        )
        verdict, why = parse_reply(res.text)
    except Exception as e:  # noqa: BLE001 - the gate must never block the user's task
        verdict, why = None, f"classifier call failed: {type(e).__name__}"
    if verdict is None:
        verdict = fallback_verdict(why)
    return apply_floor(verdict, prompt)


class ScopeLog:
    """``scope_log.jsonl``: one verdict line per task, then one outcome line for it."""

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / "scope_log.jsonl"

    def _append(self, rec: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def verdict(self, prompt: str, verdict: ScopeVerdict, session_id: str = "") -> str:
        h = prompt_hash(prompt)
        self._append({"ts": time.time(), "type": "verdict", "hash": h, "session": session_id,
                      "verdict": verdict.as_dict()})
        return h

    def outcome(self, h: str, outcome: str, **extra: Any) -> None:
        self._append({"ts": time.time(), "type": "outcome", "hash": h, "outcome": outcome, **extra})

    def read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
