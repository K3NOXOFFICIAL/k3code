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

CLASSIFIER_SYSTEM = (
    "You classify a coding task before any work starts. Reply with ONE JSON object and nothing else:\n"
    '{"scope": "trivial|small|medium|large|huge", "needs_plan": bool, "risk": "low|med|high", '
    '"parallelizable": bool, "suggested_subtasks": [str], "reason": str}\n'
    "trivial = one-line/answer-only; small = one file; medium = a few files; large = many files or a "
    "feature; huge = a project-wide effort. If the likely actions delete data, migrate, deploy, touch "
    "credentials or force-push, set needs_plan=true and risk=high. Be conservative."
)


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


def parse_verdict(text: str) -> ScopeVerdict | None:
    """Parse the classifier's JSON (tolerating prose or code fences around it)."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    scope = str(data.get("scope", "")).lower()
    if scope not in SCOPES:
        return None
    risk = str(data.get("risk", "low")).lower()
    subtasks = data.get("suggested_subtasks") or []
    return ScopeVerdict(
        scope=scope,
        needs_plan=bool(data.get("needs_plan", False)),
        risk=risk if risk in RISKS else "med",
        parallelizable=bool(data.get("parallelizable", False)),
        suggested_subtasks=[str(s) for s in subtasks if s] if isinstance(subtasks, list) else [],
        reason=str(data.get("reason", "")),
    )


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
        out = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=5)
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
        verdict = parse_verdict(res.text)
    except Exception:  # noqa: BLE001 - the gate must never block the user's task
        verdict = None
    if verdict is None:
        verdict = ScopeVerdict(scope="small", source="fallback", reason="classifier unavailable; heuristics only")
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
