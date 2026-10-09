"""Project preparation: detect the project's stacks, keep them in k3code's own state, offer opt-in proposals.

On a session's first open of a project, and whenever its marker files change (a new stack, an edited manifest), the
project is scanned (:mod:`k3code.learning.stacks`), the result is stored in
``$K3CODE_HOME/projects/<project>/project.json`` (:mod:`k3code.learning.projectstate`) and the recipes of stacks not
seen before are offered as proposals (:mod:`k3code.learning.recipes`). Nothing is written into the repository except
by the explicit "create K3CODE.md" proposal.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.learning import projectstate, recipes, stacks
from k3code.learning.decisions import project_id
from k3code.memory import PROJECT_FILES
from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

SKIP_DIRS = stacks.SKIP_DIRS
SECRET_RES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "aws-key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "api-token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|xox[bap]-[A-Za-z0-9-]{10,})"),
}


def detect(root: Path) -> dict[str, Any]:
    """The single-language view (language, package manager, test/lint/build commands, CI, monorepo, docker), derived
    from the multi-stack scan (:func:`k3code.learning.stacks.scan`)."""
    root = Path(root)
    return stacks.legacy_info(stacks.scan(root), root)


def scan_risks(root: Path, info: dict[str, Any], limit: int = 300) -> list[str]:
    """Risk flags; secret hits name the rule and file only, never the matched text."""
    risks: list[str] = []
    if not info.get("test"):
        risks.append("no test command detected")
    else:
        has_tests = any(
            p.name.startswith("test")
            or p.name.endswith(("_test.go", ".test.ts", ".test.js", ".spec.ts"))
            or p.parent.name in ("tests", "test", "__tests__")
            for p in _walk(root, 400)
        )
        if not has_tests:
            risks.append("test command exists but no test files were found")
    if not info.get("ci"):
        risks.append("no CI configuration")
    n = 0
    for p in _walk(root, 2000):
        if n >= limit:
            break
        try:
            if p.stat().st_size > 200_000:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        n += 1
        if (p.name == ".env" or p.name.startswith(".env.")) and not p.name.endswith(
            (".example", ".sample", ".template")
        ):
            risks.append(f"secrets file committed? {p.relative_to(root)}")
        for name, rx in SECRET_RES.items():
            if rx.search(text):
                risks.append(f"possible {name} in {p.relative_to(root)}")
                break
    return list(dict.fromkeys(risks))


def _walk(root: Path, cap: int) -> list[Path]:
    out: list[Path] = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for f in fns:
            out.append(Path(dp) / f)
            if len(out) >= cap:
                return out
    return out


def project_json_path(root: Path) -> Path:
    """The project's state file (outside the repository)."""
    return projectstate.state_path(root)


def needs_prep(root: Path) -> bool:
    """True when the project was never prepared or its marker files changed since (a new stack, edited manifests)."""
    state = projectstate.load(root)
    if not state.get("fingerprint"):
        return True
    return stacks.scan(projectstate.project_root(root)).fingerprint != state["fingerprint"]


DRAFT_SYSTEM = (
    "Draft a concise K3CODE.md (project memory for a coding agent) from the facts and file excerpts. Sections: "
    "Build/test commands, Conventions. Max 25 lines, markdown, no secrets, nothing you cannot see in the input."
)


def _excerpts(root: Path, limit: int = 3000) -> str:
    parts = []
    for name in ("README.md", "pyproject.toml", "package.json", "Makefile", "go.mod", "Cargo.toml"):
        p = root / name
        if p.is_file():
            parts.append(f"--- {name}\n{p.read_text(encoding='utf-8', errors='ignore')[:900]}")
    return "\n".join(parts)[:limit]


def template_memory(info: dict[str, Any]) -> str:
    lines = ["# Project notes", "", "## Build / test commands"]
    for k in ("test", "lint", "build"):
        if info.get(k):
            lines.append(f"- {k}: `{info[k]}`")
    pm = f" ({info['package_manager']})" if info["package_manager"] else ""
    lines += ["", "## Conventions", f"- Language: {info['language']}{pm}"]
    return "\n".join(lines) + "\n"


async def draft_memory(caller: Any, root: Path, info: dict[str, Any], session_id: str = "") -> str:
    if caller is None:
        return template_memory(info)
    try:
        res = await caller.complete(
            TaskKind.CLASSIFICATION,
            [
                Message(role="system", content=DRAFT_SYSTEM),
                Message(role="user", content=f"Facts: {json.dumps(info)}\n\n{_excerpts(root)}"),
            ],
            session_id=session_id,
            max_tokens=700,
            timeout=40,
        )
        text = (res.text or "").strip()
        return text + "\n" if len(text) > 20 else template_memory(info)
    except Exception:  # noqa: BLE001
        return template_memory(info)


def safe_commands(info: dict[str, Any]) -> list[str]:
    """Allow-rule patterns for the detected test/lint/build commands: ``<prefix> *``, or the exact command when its
    arguments name what runs (``uv run pytest``, ``npx eslint .``: ``uv *`` would allow any program)."""
    from k3code.permissions.engine import exact_rule_pattern
    from k3code.permissions.hardline import command_prefix, wildcard_unsafe

    cmds = [info[k] for k in ("test", "lint", "build") if info.get(k)]
    out = []
    for c in cmds:
        pattern = exact_rule_pattern(c) if wildcard_unsafe(c) else command_prefix(c) + " *"
        if pattern:
            out.append(pattern)
    return list(dict.fromkeys(out))


def _previous(root: Path) -> dict[str, Any]:
    """The stored state; else, once, what an older version wrote into the repository (read, never written)."""
    state = projectstate.load(root)
    if state:
        return state
    legacy = projectstate.legacy_path(root)
    if legacy.is_file():
        return {"migrated_from": str(legacy)}
    return {}


def _skills(root: Path, roots: list[str] | None) -> list[Any]:
    from k3code.skills import discover

    try:
        return discover(root, roots)
    except OSError:
        return []


async def prepare(
    root: Path,
    *,
    store: ProposalStore,
    caller: Any = None,
    preferences: list[str] | None = None,
    session_id: str = "",
    clock: Any = time.time,
    skill_roots: list[str] | None = None,
) -> list[Proposal]:
    """Scan the project; when it is new or its markers changed, store the result and propose what is new.

    The first preparation also offers the project-wide proposals (K3CODE.md, nightly tests, risks); a re-scan only
    offers the recipes of stacks that were not there before. The scans run in a worker thread. ``caller`` and
    ``preferences`` are unused at scan time (the K3CODE.md draft is written on acceptance)."""
    root = projectstate.project_root(root)
    scan = await asyncio.to_thread(stacks.scan, root)
    prev = _previous(root)
    if prev.get("fingerprint") == scan.fingerprint:
        return []
    if not scan.stacks and not (root / ".git").exists():
        return []  # not a project (empty or scratch directory): nothing to prepare
    info = stacks.legacy_info(scan, root)
    risks = await asyncio.to_thread(scan_risks, root, info)
    skills = await asyncio.to_thread(_skills, root, skill_roots)
    pid = project_id(root)
    known = set(prev.get("proposed") or [])
    new = [s for s in scan.stacks if stacks.stack_key(s) not in known]
    state = {
        **{k: v for k, v in prev.items() if k in ("accepted", "migrated_from")},
        "version": projectstate.VERSION,
        "root": str(root),
        "project": pid,
        "stacks": scan.stacks,
        "markers": scan.markers,
        "fingerprint": scan.fingerprint,
        "info": info,
        "risks": risks,
        "detected_at": clock(),
        "proposed": sorted(known | {stacks.stack_key(s) for s in scan.stacks}),
    }
    await asyncio.to_thread(projectstate.save, root, state)
    out: list[Proposal] = []

    def add(kind: str, text: str, action: str, payload: dict[str, Any], key: str = "") -> None:
        p = store.add(
            kind, text, action, session_id, payload=payload, project=pid, key=dedup_key(kind, f"{pid} {key or text}")
        )
        if p is not None:
            out.append(p)

    if not prev:  # first preparation (a migrated project had these offered by the older version)
        if not any((root / n).is_file() for n in PROJECT_FILES):  # K3CODE.md, AGENTS.md or CLAUDE.md
            # drafted on the cheap tier when accepted (no model call on session start); the only repository write
            add(
                "project_setup",
                "Create a K3CODE.md with this project's build/test commands and conventions?",
                "write K3CODE.md",
                {"op": "draft_memory", "path": str(root / "K3CODE.md"), "root": str(root), "info": info},
            )
        if info.get("test"):
            add(
                "project_setup",
                f"Run `{info['test']}` every night and report failures?",
                f"/schedule add nightly tests: run {info['test']} at 02:00",
                {"op": "send"},
            )
        if risks:
            add("project_setup", "Project risks found: " + "; ".join(risks[:4]), "acknowledge", {"op": "ack"})
    for stack in new:
        for s in recipes.suggest(stack, root, skills):
            add(s.kind, s.text, s.action, s.payload, s.key)
    return out


FACTS_MAX_LINES = 25


def facts_prompt(cwd: str | Path) -> str:
    """The "Project facts" system-prompt section from the stored scan (never a scan here): stacks, package managers
    and commands. Built only from k3code's own templates and plain directory names, so no project-file text reaches
    the prompt; it changes only when a re-scan stores a different result or the user confirms commands."""
    state = projectstate.load(cwd)
    found = [s for s in state.get("stacks") or [] if isinstance(s, dict) and s.get("id")]
    if not found:
        return ""
    confirmed = (state.get("accepted") or {}).get("commands") or {}
    body = ["stacks: " + ", ".join(recipes.label(s) for s in found)]
    for s in found:
        key = stacks.stack_key(s)
        cmds = confirmed.get(key) or s.get("commands") or {}
        if not cmds:
            continue
        where = "" if s.get("dir", ".") == "." else f"{s['dir']}/ "
        tag = " (confirmed)" if key in confirmed else ""
        shown = " · ".join(f"{k} `{cmds[k]}`" for k in stacks.COMMAND_KINDS if cmds.get(k))
        body.append(f"{where}{s['id']}{tag}: {shown}")
    head = ["## Project facts", "", "Detected from this project's files (re-scanned when they change):", "```text"]
    room = FACTS_MAX_LINES - len(head) - 1
    if len(body) > room:
        body = [*body[: room - 1], f"…and {len(body) - room + 1} more"]
    return "\n".join([*head, *body, "```"])


def pending_recipes(store: ProposalStore, root: Path) -> list[Proposal]:
    """Recipe proposals of this project that still wait for an answer."""
    root_s = str(projectstate.project_root(root))
    return [
        p
        for p in store.all()
        if p.status == "pending" and p.payload.get("op") == "recipe" and p.payload.get("root") == root_s
    ]


async def apply(payload: dict[str, Any], caller: Any = None, session_id: str = "") -> str:
    """Run the accepted project_setup or recipe operation (writes only after acceptance)."""
    op = payload.get("op")
    if op == "draft_memory":
        payload = {
            "op": "write_file",
            "path": payload["path"],
            "content": await draft_memory(caller, Path(payload["root"]), payload["info"], session_id),
        }
        op = "write_file"
    if op == "write_file":
        path = Path(payload["path"])
        if path.exists():
            return f"{path.name} already exists; left unchanged"
        path.write_text(payload["content"], encoding="utf-8")
        return f"wrote {path}"
    if op == "allow_rules":  # a proposal made before rules moved out of the repository
        from k3code.permissions.rules import Rule
        from k3code.permissions.state import persist_rules

        path = projectstate.rules_path(Path(payload["cwd"]))
        persist_rules(path, [Rule(**r) for r in payload["rules"]])
        return f"allow rules added for this project ({path})"
    if op == "recipe":
        return recipes.apply(payload)
    return "noted"
