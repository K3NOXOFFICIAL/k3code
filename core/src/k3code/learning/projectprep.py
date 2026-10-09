"""Project preparation on first open: detect the stack, write ``.k3code/project.json``, offer proposals."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.learning.decisions import project_id
from k3code.memory import PROJECT_FILES
from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "dist", "build", "__pycache__", ".k3code"}
SECRET_RES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "aws-key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "api-token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|xox[bap]-[A-Za-z0-9-]{10,})"),
}


def _load_json(p: Path) -> dict[str, Any]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _has(root: Path, *names: str) -> bool:
    return any((root / n).exists() for n in names)


def detect(root: Path) -> dict[str, Any]:
    """Detect language, package manager and the test/lint/build commands."""
    root = Path(root)
    info: dict[str, Any] = {
        "language": "unknown",
        "package_manager": "",
        "test": "",
        "lint": "",
        "build": "",
        "ci": "",
        "monorepo": False,
        "docker": False,
        "makefile": (root / "Makefile").is_file(),
    }
    if _has(root, "pyproject.toml", "setup.py", "requirements.txt"):
        info["language"] = "python"
        pyp = root / "pyproject.toml"
        py = pyp.read_text(encoding="utf-8", errors="ignore") if pyp.is_file() else ""
        if _has(root, "uv.lock") or "[tool.uv]" in py:
            info["package_manager"], run = "uv", "uv run "
        elif _has(root, "poetry.lock") or "[tool.poetry]" in py:
            info["package_manager"], run = "poetry", "poetry run "
        else:
            info["package_manager"], run = "pip", ""
        info["test"] = f"{run}pytest"
        if "ruff" in py or _has(root, "ruff.toml", ".ruff.toml"):
            info["lint"] = f"{run}ruff check ."
        if "[build-system]" in py:
            info["build"] = "uv build" if info["package_manager"] == "uv" else "python -m build"
    elif (root / "package.json").is_file():
        pkg = _load_json(root / "package.json")
        info["language"] = "typescript" if (_has(root, "tsconfig.json")) else "javascript"
        pm = "pnpm" if _has(root, "pnpm-lock.yaml") else "yarn" if _has(root, "yarn.lock") else "npm"
        info["package_manager"] = pm
        scripts = pkg.get("scripts") or {}
        run = f"{pm} run "
        for key, name in (("test", "test"), ("lint", "lint"), ("build", "build")):
            if name in scripts:
                info[key] = f"{pm} test" if name == "test" else f"{run}{name}"
        if pkg.get("workspaces"):
            info["monorepo"] = True
    elif (root / "go.mod").is_file():
        info.update(
            language="go", package_manager="go", test="go test ./...", build="go build ./...", lint="go vet ./..."
        )
    elif (root / "Cargo.toml").is_file():
        info.update(
            language="rust", package_manager="cargo", test="cargo test", build="cargo build", lint="cargo clippy"
        )
        if "[workspace]" in (root / "Cargo.toml").read_text(encoding="utf-8", errors="ignore"):
            info["monorepo"] = True
    if info["makefile"] and not info["test"]:
        mk = (root / "Makefile").read_text(encoding="utf-8", errors="ignore")
        for key in ("test", "lint", "build"):
            if re.search(rf"^{key}\s*:", mk, re.M):
                info[key] = f"make {key}"
    if (root / ".github" / "workflows").is_dir():
        info["ci"] = "github-actions"
    elif _has(root, ".gitlab-ci.yml"):
        info["ci"] = "gitlab-ci"
    elif _has(root, ".circleci"):
        info["ci"] = "circleci"
    info["docker"] = _has(root, "Dockerfile", "docker-compose.yml", "compose.yaml", "docker-compose.yaml")
    if _has(root, "pnpm-workspace.yaml", "lerna.json", "nx.json", "turbo.json"):
        info["monorepo"] = True
    return info


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
    return Path(root) / ".k3code" / "project.json"


def needs_prep(root: Path) -> bool:
    return not project_json_path(root).is_file()


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


async def prepare(
    root: Path,
    *,
    store: ProposalStore,
    caller: Any = None,
    preferences: list[str] | None = None,
    session_id: str = "",
    clock: Any = time.time,
) -> list[Proposal]:
    """Detect + write ``project.json`` (the only unconditional write), then create proposals."""
    root = Path(root)
    info = detect(root)
    if info["language"] == "unknown" and not (root / ".git").exists() and not info["docker"]:
        return []  # not a project (empty or scratch directory): nothing to prepare
    risks = scan_risks(root, info)
    meta = {**info, "risks": risks, "detected_at": clock(), "project": project_id(root)}
    path = project_json_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    pid = meta["project"]
    out: list[Proposal] = []

    def add(kind: str, text: str, action: str, payload: dict[str, Any]) -> None:
        p = store.add(
            kind, text, action, session_id, payload=payload, project=pid, key=dedup_key(kind, f"{pid} {text}")
        )
        if p is not None:
            out.append(p)

    if not any((root / n).is_file() for n in PROJECT_FILES):  # K3CODE.md, AGENTS.md or CLAUDE.md
        # drafted on the cheap tier when accepted (no model call on session start)
        add(
            "project_setup",
            "Create a K3CODE.md with this project's build/test commands and conventions?",
            "write K3CODE.md",
            {"op": "draft_memory", "path": str(root / "K3CODE.md"), "root": str(root), "info": info},
        )
    pats = safe_commands(info)
    if pats:
        add(
            "project_setup",
            f"Allow the detected safe commands without asking ({', '.join(pats)})?",
            "add allow rules",
            {
                "op": "allow_rules",
                "cwd": str(root),
                "rules": [{"tool": "bash", "pattern": p, "action": "allow"} for p in pats],
            },
        )
    if info.get("test"):
        add(
            "project_setup",
            f"Run `{info['test']}` every night and report failures?",
            f"/schedule add nightly tests: run {info['test']} at 02:00",
            {"op": "send"},
        )
    if preferences and any("makefile" in p.lower() for p in preferences) and not info["makefile"]:
        targets = [f"{k}:\n\t{info[k]}" for k in ("test", "lint", "build") if info.get(k)]
        add(
            "project_setup",
            "You usually add a Makefile; create one with test/lint/build targets?",
            "write Makefile",
            {"op": "write_file", "path": str(root / "Makefile"), "content": "\n\n".join(targets) + "\n"},
        )
    if risks:
        add("project_setup", "Project risks found: " + "; ".join(risks[:4]), "acknowledge", {"op": "ack"})
    return out


async def apply(payload: dict[str, Any], caller: Any = None, session_id: str = "") -> str:
    """Run the accepted project_setup operation (writes only after acceptance)."""
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
    if op == "allow_rules":
        from k3code.permissions.rules import Rule
        from k3code.permissions.state import persist_rules, project_config_path

        persist_rules(project_config_path(Path(payload["cwd"])), [Rule(**r) for r in payload["rules"]])
        return "allow rules added to the project config"
    return "noted"
