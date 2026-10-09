"""Project recipes: what a detected stack suggests (skills, MCP servers, hooks, allow rules, commands).

Every suggestion becomes one proposal card. Nothing is enabled until the user accepts it, and accepting writes only
k3code's own state: the user's config.yaml (hooks, scoped to the project path) or the project's directory under
``$K3CODE_HOME/projects/`` (rules, MCP servers, pinned skills, confirmed commands). Never a file in the repository.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k3code.learning import projectstate

#: kinds of recipe proposals (``Proposal.kind``); their payload carries ``op: "recipe"``
KINDS = ("skill", "mcp", "hook", "rule", "commands")

#: MCP servers a recipe may suggest. Secrets are never written: ``bearer_env`` names the variable that holds one.
MCP_SERVERS: dict[str, dict[str, Any]] = {
    "docs": {
        "name": "context7",
        "spec": {"command": "npx", "args": ["-y", "@upstash/context7-mcp"]},
        "why": "current, version-specific documentation for the libraries the project uses",
    },
    "nixos": {
        "name": "nixos",
        "spec": {"command": "uvx", "args": ["mcp-nixos"]},
        "why": "look up NixOS options and packages instead of guessing them",
    },
    "home-assistant": {
        "name": "home-assistant",
        "spec": {"url": "{url}/api/mcp", "bearer_env": "HASS_TOKEN"},
        "why": "query and control the Home Assistant instance this configuration belongs to (its MCP Server "
        "integration must be enabled)",
        "default_url": "http://homeassistant.local:8123",
    },
}

_PRETTIER_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".css", ".scss", ".md", ".html", ".vue")
_PRETTIER_EXEC = {
    "npm": "npx --no-install prettier --write",
    "pnpm": "pnpm exec prettier --write",
    "yarn": "yarn prettier --write",
    "bun": "bunx prettier --write",
}

#: stack id -> recipe. ``keywords`` match skills (name and description) and rank the skills index;
#: ``mcp`` names MCP_SERVERS entries; ``hooks`` are PostToolUse formatters (``when`` names a stack ``extra`` flag
#: that must be true); ``rules`` are the command kinds offered as exact allow rules.
RECIPES: dict[str, dict[str, Any]] = {
    "python": {
        "keywords": ("python", "pytest", "ruff", "uv", "poetry", "pip", "django", "flask", "fastapi", "mypy"),
        "mcp": ("docs",),
        "hooks": ({"formatter": "{run}ruff format", "exts": (".py", ".pyi"), "when": "ruff"},),
        "rules": ("test", "lint"),
    },
    "node": {
        "keywords": (
            "javascript",
            "typescript",
            "node",
            "npm",
            "pnpm",
            "yarn",
            "bun",
            "deno",
            "react",
            "vue",
            "svelte",
            "jest",
            "vitest",
            "eslint",
            "prettier",
        ),
        "mcp": ("docs",),
        "hooks": ({"formatter": "{prettier}", "exts": _PRETTIER_EXTS, "when": "prettier"},),
        "rules": ("test", "lint"),
    },
    "go": {
        "keywords": ("go", "golang"),
        "mcp": ("docs",),
        "hooks": ({"formatter": "gofmt -w", "exts": (".go",)},),
        "rules": ("test", "lint"),
    },
    "rust": {
        "keywords": ("rust", "cargo", "clippy"),
        "mcp": ("docs",),
        "hooks": ({"formatter": "rustfmt{edition}", "exts": (".rs",)},),
        "rules": ("test", "lint"),
    },
    "java": {
        "keywords": ("java", "maven", "gradle", "spring", "junit", "kotlin"),
        "mcp": ("docs",),
        "rules": ("test",),
    },
    "ruby": {"keywords": ("ruby", "rails", "rspec", "bundler", "rubocop"), "mcp": ("docs",), "rules": ("test", "lint")},
    "php": {
        "keywords": ("php", "composer", "laravel", "symfony", "phpunit"),
        "mcp": ("docs",),
        "rules": ("test", "lint"),
    },
    "dotnet": {"keywords": ("dotnet", "csharp", "nuget"), "mcp": ("docs",), "rules": ("test",)},
    "nix": {
        "keywords": ("nix", "nixos", "flake"),
        "mcp": ("nixos",),
        "hooks": ({"formatter": "nixfmt", "exts": (".nix",)},),
        "rules": ("test",),
    },
    "docker": {"keywords": ("docker", "dockerfile", "compose", "container")},
    "home-assistant": {
        "keywords": ("home-assistant", "homeassistant", "hass", "esphome"),
        "mcp": ("home-assistant",),
        "rules": ("lint",),
    },
    "terraform": {"keywords": ("terraform", "hcl", "opentofu"), "rules": ("lint",)},
    "ansible": {"keywords": ("ansible", "playbook"), "rules": ("lint",)},
    "make": {"keywords": ("make", "makefile", "just", "justfile"), "rules": ("test", "lint")},
    "ci": {"keywords": ("ci", "github-actions", "gitlab", "woodpecker")},
}

#: keywords too common in prose to count in a skill's description (they still count in its name)
_NAME_ONLY = frozenset({"go", "make", "just", "ci", "node", "uv", "pip", "bun", "hass"})
MAX_SKILLS = 3


def keywords_for(stack_ids: list[str] | set[str]) -> set[str]:
    return {k for sid in stack_ids for k in RECIPES.get(sid, {}).get("keywords", ())}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9#+.]+(?:-[a-z0-9]+)*", text.lower()))


def skill_score(name: str, description: str, keywords: set[str]) -> int:
    """3 per keyword in the skill's name, 1 per (non-generic) keyword in its description."""
    name_l = name.lower()
    name_words = set(re.split(r"[-_ .]+", name_l)) | {name_l}
    desc = _words(description)
    score = 0
    for k in keywords:
        if k in name_words or ("-" in k and k in name_l):
            score += 3
        elif k not in _NAME_ONLY and k in desc:
            score += 1
    return score


def matching_skills(skills: list[Any], stack_id: str) -> list[str]:
    """Discovered skills that fit a stack (score >= 2: a keyword in the name, or two in the description)."""
    kws = keywords_for([stack_id])
    scored = [(skill_score(s.name, s.description, kws), s.name) for s in skills]
    return [n for sc, n in sorted(scored, key=lambda t: (-t[0], t[1])) if sc >= 2][:MAX_SKILLS]


# ── formatter hooks ──

#: double quotes only, so the single-quoted shell word stays readable in config.yaml
_PATH_OF_EDIT = (
    'import json,os,sys;d=os.path.realpath(sys.argv[1]);p=str((json.load(sys.stdin).get("tool_input") or {})'
    '.get("path") or "");p=os.path.realpath(os.path.expanduser(p)) if p else "";'
    "print(p) if p.endswith(tuple(sys.argv[2:])) and os.path.commonpath([d,p])==d else None"
)


def formatter_hook_command(directory: str | Path, formatter: str, exts: tuple[str, ...]) -> str:
    """A shell command for a PostToolUse hook: format the edited file (read from the hook's stdin JSON) when it has
    one of ``exts`` and lies under ``directory``; never fails the tool call. The path is absolute, so it can never
    be read as an option of the formatter."""
    d = shlex.quote(str(directory))
    ext_args = " ".join(shlex.quote(e) for e in exts)
    pick = f"python3 -c {shlex.quote(_PATH_OF_EDIT)} {d} {ext_args}"
    return f'f=$({pick}) && [ -n "$f" ] && cd {d} && {formatter} "$f"; exit 0'


def _formatter(template: str, stack: dict[str, Any]) -> str | None:
    extra = stack.get("extra") or {}
    if "{prettier}" in template:
        exec_ = _PRETTIER_EXEC.get(stack.get("manager", ""))
        return template.replace("{prettier}", exec_) if exec_ else None
    edition = f" --edition {extra['edition']}" if extra.get("edition") else ""
    return template.replace("{run}", str(extra.get("run") or "")).replace("{edition}", edition)


# ── suggestions ──


@dataclass
class Suggestion:
    kind: str
    text: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    key: str = ""  # dedup identity within the project (defaults to the text)


def label(stack: dict[str, Any]) -> str:
    """``python (uv) in api/``; ``go (go)`` at the root."""
    manager = f" ({stack['manager']})" if stack.get("manager") else ""
    where = f" in {stack['dir']}/" if stack.get("dir", ".") != "." else ""
    return f"{stack['id']}{manager}{where}"


def exact_rules(stack: dict[str, Any], kinds: tuple[str, ...]) -> list[str]:
    """Exact-command allow patterns for the stack's ``kinds`` commands; never a wildcard, never an unsafe shape."""
    from k3code.learning.permrules import unsafe_pattern
    from k3code.permissions.engine import exact_rule_pattern

    out = []
    for k in kinds:
        cmd = (stack.get("commands") or {}).get(k)
        pattern = exact_rule_pattern(cmd) if cmd else None
        if pattern and "*" not in pattern and not unsafe_pattern(pattern):
            out.append(pattern)
    return list(dict.fromkeys(out))


def suggest(stack: dict[str, Any], root: Path, skills: list[Any] | None = None) -> list[Suggestion]:
    """The recipe suggestions for one detected stack (``stacks.scan`` entry) of the project at ``root``."""
    recipe = RECIPES.get(stack["id"])
    if recipe is None:
        return []
    root = Path(root)
    base = {"op": "recipe", "root": str(root), "stack": stack["id"], "dir": stack["dir"]}
    name = label(stack)
    out: list[Suggestion] = []
    if skills and (names := matching_skills(skills, stack["id"])):
        out.append(
            Suggestion(
                "skill",
                f"{name}: pin the skills {', '.join(names)} for this project (listed first in the skills index)?",
                f"pin skills {', '.join(names)}",
                {**base, "type": "skill", "skills": names},
            )
        )
    for ref in recipe.get("mcp", ()):
        srv = MCP_SERVERS[ref]
        spec = dict(srv["spec"])
        if "url" in spec:
            url = str((stack.get("extra") or {}).get("url") or srv.get("default_url") or "")
            spec["url"] = spec["url"].replace("{url}", url.rstrip("/"))
        need = f" Needs {spec['bearer_env']} set (a token; never stored in config)." if spec.get("bearer_env") else ""
        where = f" at {spec['url']}" if spec.get("url") else f" (`{' '.join([spec['command'], *spec['args']])}`)"
        out.append(
            Suggestion(
                "mcp",
                f"{name}: add the {srv['name']} MCP server{where} for this project: {srv['why']}?{need}",
                f"add MCP server {srv['name']}",
                {**base, "type": "mcp", "name": srv["name"], "server": spec},
                key=f"mcp {srv['name']}",
            )
        )
    extra = stack.get("extra") or {}
    for hook in recipe.get("hooks", ()):
        if hook.get("when") and not extra.get(hook["when"]):
            continue
        formatter = _formatter(hook["formatter"], stack)
        if not formatter:
            continue
        directory = root if stack["dir"] == "." else root / stack["dir"]
        entry = {
            "matcher": "edit|write",
            "command": formatter_hook_command(directory, formatter, hook["exts"]),
            "timeout": 60,
            "project": str(root),
        }
        exts = "/".join(hook["exts"][:4]) + ("/…" if len(hook["exts"]) > 4 else "")
        out.append(
            Suggestion(
                "hook",
                f"{name}: run `{formatter}` on each edited {exts} file (a PostToolUse hook for this project only)?",
                f"add formatter hook {formatter}",
                {**base, "type": "hook", "event": "PostToolUse", "entry": entry},
                key=f"hook {formatter} {stack['dir']}",
            )
        )
    if pats := exact_rules(stack, tuple(recipe.get("rules", ()))):
        out.append(
            Suggestion(
                "rule",
                f"{name}: allow {', '.join(f'`{p}`' for p in pats)} without asking in this project?",
                "add allow rules",
                {**base, "type": "rule", "rules": [{"tool": "bash", "pattern": p, "action": "allow"} for p in pats]},
            )
        )
    if cmds := stack.get("commands"):
        shown = ", ".join(f"{k} `{v}`" for k, v in cmds.items())
        out.append(
            Suggestion(
                "commands",
                f"{name}: use {shown} as this project's commands?",
                "confirm project commands",
                {**base, "type": "commands", "commands": dict(cmds)},
            )
        )
    return out


# ── applying an accepted recipe proposal ──


def apply(payload: dict[str, Any]) -> str:
    """Apply an accepted recipe proposal to the user's config or the project's state under $K3CODE_HOME."""
    kind = payload.get("type")
    root = Path(str(payload.get("root") or "."))
    if kind == "skill":
        names = [str(n) for n in payload.get("skills") or []]

        def pin(state: dict[str, Any]) -> None:
            acc = projectstate.accepted(state)
            acc["skills"] = sorted({*acc["skills"], *names})

        projectstate.update(root, pin)
        return f"pinned {', '.join(names)} for this project"
    if kind == "commands":
        key = f"{payload.get('stack')}@{payload.get('dir', '.')}"
        cmds = {str(k): str(v) for k, v in (payload.get("commands") or {}).items()}

        def confirm(state: dict[str, Any]) -> None:
            projectstate.accepted(state)["commands"][key] = cmds

        projectstate.update(root, confirm)
        return "project commands confirmed"
    if kind == "rule":
        from k3code.permissions.rules import Rule
        from k3code.permissions.state import persist_rules

        rules = [Rule(**r) for r in payload.get("rules") or []]
        path = projectstate.rules_path(root)
        persist_rules(path, rules)
        return f"allow rules {', '.join(f'`{r.pattern}`' for r in rules)} added for this project ({path})"
    if kind == "mcp":
        path = projectstate.add_mcp_server(root, str(payload["name"]), dict(payload.get("server") or {}))
        env = (payload.get("server") or {}).get("bearer_env")
        tail = f"; set {env} before it can connect" if env else ""
        return f"MCP server {payload['name']} added for this project ({path}){tail}"
    if kind == "hook":
        from k3code import confio
        from k3code.paths import user_config_path

        event, entry = str(payload.get("event") or "PostToolUse"), dict(payload.get("entry") or {})
        path = user_config_path()
        data = confio.read_yaml(path)
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        entries = hooks.get(event) if isinstance(hooks.get(event), list) else []
        if any(isinstance(e, dict) and e.get("command") == entry.get("command") for e in entries):
            return "this hook is already configured"
        hooks[event] = [*entries, entry]
        data["hooks"] = hooks
        confio.write_yaml(path, data)
        return f"{event} hook added to {path} (runs in {entry.get('project')} only)"
    return "noted"
