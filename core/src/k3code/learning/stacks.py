"""Project stack detection: a marker table, every stack of a (mono)repo, and the commands each one implies.

Nothing here runs a project tool: commands are derived from files (``scripts`` in package.json, Makefile targets,
``[tool.*]`` sections of pyproject.toml). Scanned: the root, its first-level subdirectories and the directories its
workspace declarations name (package.json ``workspaces``, pnpm-workspace.yaml, Cargo/uv workspace members, go.work).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "dist", "build", "__pycache__", ".k3code"}
MAX_WORKSPACE_DIRS = 64
#: Directory reads one scan may spend expanding all of a project's workspace globs, and how deep a ``**`` descends:
#: ``packages/**`` in a very large monorepo used to walk the whole tree before the first 64 entries were kept.
MAX_WORKSPACE_VISITS = 2000
MAX_GLOB_DEPTH = 6
COMMAND_KINDS = ("test", "lint", "format", "build")


@dataclass(frozen=True)
class Marker:
    stack: str
    pattern: str  # glob over the names in one directory, or a relative path ("a/b") checked for existence
    contains: str = ""  # regex (multiline) the file must contain
    root_only: bool = False


#: marker -> stack id. Order matters only for the legacy single-language view (first language found wins).
MARKERS: tuple[Marker, ...] = (
    Marker("python", "pyproject.toml"),
    Marker("python", "uv.lock"),
    Marker("python", "poetry.lock"),
    Marker("python", "requirements*.txt"),
    Marker("python", "setup.cfg"),
    Marker("python", "setup.py"),
    Marker("node", "package.json"),
    Marker("node", "deno.json"),
    Marker("node", "deno.jsonc"),
    Marker("go", "go.mod"),
    Marker("rust", "Cargo.toml"),
    Marker("java", "pom.xml"),
    Marker("java", "build.gradle*"),
    Marker("ruby", "Gemfile"),
    Marker("php", "composer.json"),
    Marker("dotnet", "*.csproj"),
    Marker("dotnet", "*.sln"),
    Marker("nix", "flake.nix"),
    Marker("nix", "default.nix"),
    Marker("nix", "shell.nix"),
    Marker("docker", "Dockerfile"),
    Marker("docker", "compose*.y*ml"),
    Marker("docker", "docker-compose*.y*ml"),
    Marker("home-assistant", "configuration.yaml", contains=r"^(homeassistant|default_config):"),
    Marker("home-assistant", "custom_components"),
    Marker("terraform", "*.tf"),
    Marker("ansible", "ansible.cfg"),
    Marker("ansible", "playbooks"),
    Marker("make", "Makefile"),
    Marker("make", "justfile"),
    Marker("ci", ".github/workflows", root_only=True),
    Marker("ci", ".gitlab-ci.yml", root_only=True),
    Marker("ci", ".woodpecker*", root_only=True),
    Marker("ci", ".circleci", root_only=True),
)

#: stacks that are a programming language (the legacy ``language`` field picks the first of these)
LANGUAGES = ("python", "node", "go", "rust", "java", "ruby", "php", "dotnet")
_SAFE_DIR = re.compile(r"^[A-Za-z0-9._/-]+$")


def _read(p: Path, limit: int = 200_000) -> str:
    try:
        if p.stat().st_size > limit:
            return ""
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _load_json(p: Path) -> dict[str, Any]:
    try:
        d = json.loads(_read(p))
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _load_toml(p: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(_read(p))
    except (tomllib.TOMLDecodeError, ValueError):
        return {}


def _names(d: Path) -> set[str]:
    try:
        return set(os.listdir(d))
    except OSError:
        return set()


# ── commands per stack (derived from files, never executed) ──


def _python(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    py = _read(d / "pyproject.toml")
    if "uv.lock" in names or "[tool.uv" in py:
        pm, run = "uv", "uv run "
    elif "poetry.lock" in names or "[tool.poetry" in py:
        pm, run = "poetry", "poetry run "
    else:
        pm, run = "pip", ""
    ruff = "ruff" in py or bool(names & {"ruff.toml", ".ruff.toml"})
    cmds = {"test": f"{run}pytest"}
    if ruff:
        cmds["lint"], cmds["format"] = f"{run}ruff check .", f"{run}ruff format ."
    elif "[tool.black]" in py:
        cmds["format"] = f"{run}black ."
    if "[build-system]" in py:
        cmds["build"] = "uv build" if pm == "uv" else "python -m build"
    return pm, cmds, {"ruff": ruff, "run": run}


def _node(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    if "package.json" not in names:  # deno
        return "deno", {"test": "deno test", "lint": "deno lint", "format": "deno fmt"}, {}
    pkg = _load_json(d / "package.json")
    if "pnpm-lock.yaml" in names:
        pm = "pnpm"
    elif "yarn.lock" in names:
        pm = "yarn"
    elif names & {"bun.lockb", "bun.lock"}:
        pm = "bun"
    elif "package-lock.json" in names:
        pm = "npm"
    else:
        declared = str(pkg.get("packageManager") or "").split("@", 1)[0]
        pm = declared if declared in ("pnpm", "yarn", "bun", "npm") else "npm"
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    cmds: dict[str, str] = {}
    if "test" in scripts:
        cmds["test"] = "bun run test" if pm == "bun" else f"{pm} test"
    for key in ("lint", "format", "build"):
        if key in scripts:
            cmds[key] = f"{pm} run {key}"
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})} if isinstance(pkg, dict) else {}
    prettier = (
        "prettier" in pkg or "prettier" in deps or any(n.startswith((".prettierrc", "prettier.config.")) for n in names)
    )
    extra = {"typescript": "tsconfig.json" in names, "prettier": prettier, "workspaces": bool(pkg.get("workspaces"))}
    return pm, cmds, extra


def _go(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    return (
        "go",
        {"test": "go test ./...", "lint": "go vet ./...", "format": "gofmt -w .", "build": "go build ./..."},
        {},
    )


def _rust(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    cargo = _load_toml(d / "Cargo.toml")
    edition = str((cargo.get("package") or {}).get("edition") or "") if isinstance(cargo.get("package"), dict) else ""
    if not re.fullmatch(r"\d{4}", edition):  # repo-controlled and spliced into a hook command: a year or nothing
        edition = ""
    cmds = {"test": "cargo test", "lint": "cargo clippy", "format": "cargo fmt", "build": "cargo build"}
    return "cargo", cmds, {"edition": edition, "workspace": "workspace" in cargo}


def _java(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    if "pom.xml" in names:
        return "maven", {"test": "mvn test", "build": "mvn package"}, {}
    g = "./gradlew" if "gradlew" in names else "gradle"
    return "gradle", {"test": f"{g} test", "build": f"{g} build"}, {}


def _ruby(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    gemfile = _read(d / "Gemfile")
    cmds: dict[str, str] = {}
    if "rspec" in gemfile or "spec" in names:
        cmds["test"] = "bundle exec rspec"
    elif "Rakefile" in names:
        cmds["test"] = "bundle exec rake test"
    if "rubocop" in gemfile:
        cmds["lint"] = "bundle exec rubocop"
    return "bundler", cmds, {}


def _php(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    composer = _load_json(d / "composer.json")
    scripts = composer.get("scripts") if isinstance(composer.get("scripts"), dict) else {}
    cmds: dict[str, str] = {}
    if "test" in scripts:
        cmds["test"] = "composer test"
    elif "phpunit" in json.dumps(composer):
        cmds["test"] = "vendor/bin/phpunit"
    if "lint" in scripts:
        cmds["lint"] = "composer lint"
    return "composer", cmds, {}


def _dotnet(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    return "dotnet", {"test": "dotnet test", "format": "dotnet format", "build": "dotnet build"}, {}


def _nix(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    if "flake.nix" in names:
        return "flake", {"test": "nix flake check", "format": "nix fmt", "build": "nix build"}, {}
    if "default.nix" in names:
        return "nix", {"build": "nix-build"}, {}
    return "nix", {}, {}


def _docker(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    if any(fnmatch.fnmatch(n, "compose*.y*ml") or fnmatch.fnmatch(n, "docker-compose*.y*ml") for n in names):
        return "compose", {"build": "docker compose build"}, {}
    return "dockerfile", {"build": "docker build ."}, {}


_HA_URL = re.compile(r"^\s*internal_url:\s*[\"']?(https?://[^\s\"']+)", re.M)


def _home_assistant(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    m = _HA_URL.search(_read(d / "configuration.yaml"))
    return "", {"lint": "hass --script check_config -c ."}, {"url": m.group(1).rstrip("/") if m else ""}


def _terraform(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    return "terraform", {"lint": "terraform validate", "format": "terraform fmt"}, {}


def _ansible(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    return "ansible", {"lint": "ansible-lint"}, {}


_TARGETS = {"test": "test", "lint": "lint", "format": "format", "fmt": "format", "build": "build"}


def _make(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    tool, text = ("make", _read(d / "Makefile")) if "Makefile" in names else ("just", _read(d / "justfile"))
    targets: set[str] = set()
    # rule lines (`test:`, `fmt build:`, just's `test *args:`), not assignments (`x := 1`) or recipe bodies
    for line in re.findall(r"^([A-Za-z0-9_.][^:=#\n]*):(?!=)", text, re.M):
        words = line.split()
        targets.update(words[:1] if tool == "just" else words)  # just: the words after the name are parameters
    cmds: dict[str, str] = {}
    for target, kind in _TARGETS.items():
        if kind not in cmds and target in targets:
            cmds[kind] = f"{tool} {target}"
    return tool, cmds, {}


def _ci(d: Path, names: set[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
    kinds = []
    if (d / ".github" / "workflows").is_dir():
        kinds.append("github-actions")
    if ".gitlab-ci.yml" in names:
        kinds.append("gitlab-ci")
    if any(n.startswith(".woodpecker") for n in names):
        kinds.append("woodpecker")
    if ".circleci" in names:
        kinds.append("circleci")
    return ",".join(kinds), {}, {}


DERIVE = {
    "python": _python,
    "node": _node,
    "go": _go,
    "rust": _rust,
    "java": _java,
    "ruby": _ruby,
    "php": _php,
    "dotnet": _dotnet,
    "nix": _nix,
    "docker": _docker,
    "home-assistant": _home_assistant,
    "terraform": _terraform,
    "ansible": _ansible,
    "make": _make,
    "ci": _ci,
}


# ── scanning ──


def _matches(d: Path, names: set[str], m: Marker) -> list[str]:
    if "/" in m.pattern:
        return [m.pattern] if (d / m.pattern).exists() else []
    hits = sorted(n for n in names if fnmatch.fnmatchcase(n, m.pattern))
    if m.contains:
        rx = re.compile(m.contains, re.M)
        hits = [n for n in hits if (d / n).is_file() and rx.search(_read(d / n))]
    return hits


def _workspace_globs(root: Path, names: set[str]) -> list[str]:
    globs: list[str] = []
    if "package.json" in names:
        ws = _load_json(root / "package.json").get("workspaces")
        ws = ws.get("packages") if isinstance(ws, dict) else ws
        globs += [str(g) for g in ws or [] if isinstance(g, str)] if isinstance(ws, list) else []
    if "pnpm-workspace.yaml" in names:
        try:
            data = yaml.safe_load(_read(root / "pnpm-workspace.yaml")) or {}
        except yaml.YAMLError:
            data = {}
        pk = data.get("packages") if isinstance(data, dict) else None
        globs += [str(g) for g in pk or [] if isinstance(g, str)] if isinstance(pk, list) else []
    if "Cargo.toml" in names:
        members = (_load_toml(root / "Cargo.toml").get("workspace") or {}).get("members")
        globs += [str(g) for g in members or [] if isinstance(g, str)] if isinstance(members, list) else []
    if "pyproject.toml" in names:
        tool = _load_toml(root / "pyproject.toml").get("tool") or {}
        members = ((tool.get("uv") or {}).get("workspace") or {}).get("members") if isinstance(tool, dict) else None
        globs += [str(g) for g in members or [] if isinstance(g, str)] if isinstance(members, list) else []
    if "go.work" in names:
        text = _read(root / "go.work")
        for block in re.findall(r"^use\s*\(([^)]*)\)", text, re.M):
            globs += block.split()
        globs += re.findall(r"^use\s+([^\s(]+)", text, re.M)
    return [g for g in globs if g and not g.startswith("!")]


def _subdirs(d: Path, budget: list[int], follow_links: bool) -> list[Path]:
    """Visible subdirectories of ``d``, sorted, minus SKIP_DIRS; each call spends one directory read of ``budget``."""
    if budget[0] <= 0:
        return []
    budget[0] -= 1
    try:
        with os.scandir(d) as it:
            found = [
                Path(e.path)
                for e in it
                if not e.name.startswith(".")
                and e.name not in SKIP_DIRS
                and (follow_links or not e.is_symlink())
                and e.is_dir()
            ]
    except OSError:
        return []
    return sorted(found)


def _glob_dirs(root: Path, pattern: str, budget: list[int]) -> list[Path]:
    """Directories below ``root`` that a workspace glob names, at most MAX_WORKSPACE_DIRS.

    Like ``Path.glob`` for the directory patterns a workspace file uses (``*``, ``?``, ``[..]``, ``**``), but it never
    reads into SKIP_DIRS, hidden or (for ``**``) symlinked directories, descends at most MAX_GLOB_DEPTH levels for a
    ``**`` and stops once ``budget`` (shared by every glob of one scan) is spent.
    """
    segments = [seg for seg in pattern.split("/") if seg and seg != "."]
    found: list[Path] = []

    def walk(d: Path, i: int, depth: int) -> None:
        if len(found) >= MAX_WORKSPACE_DIRS:
            return
        if i == len(segments):
            found.append(d)
            return
        seg = segments[i]
        if seg == "**":
            walk(d, i + 1, depth)  # ** also matches no directory at all
            if depth < MAX_GLOB_DEPTH:
                for child in _subdirs(d, budget, follow_links=False):
                    walk(child, i, depth + 1)
        elif not any(c in seg for c in "*?["):
            child = d / seg
            if child.is_dir() and seg not in SKIP_DIRS:
                walk(child, i + 1, depth)
        else:
            for child in _subdirs(d, budget, follow_links=True):
                if fnmatch.fnmatchcase(child.name, seg):
                    walk(child, i + 1, depth)

    walk(root, 0, 0)
    return found


def _scan_dirs(root: Path, names: set[str]) -> list[str]:
    """Relative directories to scan: ".", first-level subdirectories, workspace members (inside the root only)."""
    dirs = {"."}
    for n in names:
        if n.startswith(".") or n in SKIP_DIRS:
            continue
        if (root / n).is_dir() and not (root / n).is_symlink():
            dirs.add(n)
    resolved_root = root.resolve()
    extra: set[str] = set()
    budget = [MAX_WORKSPACE_VISITS]
    for g in _workspace_globs(root, names):
        g = g.strip().removeprefix("./").rstrip("/")
        if not g or g.startswith("/") or ".." in Path(g).parts:
            continue
        for p in _glob_dirs(root, g, budget):
            try:
                rel = p.resolve().relative_to(resolved_root)
            except ValueError:
                continue
            if not any(part in SKIP_DIRS for part in rel.parts):
                extra.add(rel.as_posix())
            if len(extra) >= MAX_WORKSPACE_DIRS:
                break
        if len(extra) >= MAX_WORKSPACE_DIRS:
            break
    return sorted(dirs | extra, key=lambda d: (d != ".", d))


@dataclass
class Scan:
    stacks: list[dict[str, Any]]
    markers: list[str]  # sorted relative marker paths
    fingerprint: str
    workspaces: bool

    def ids(self) -> list[str]:
        return list(dict.fromkeys(s["id"] for s in self.stacks))


def stack_key(stack: dict[str, Any]) -> str:
    """Identity of one detected stack: ``python@backend`` (``@.`` for the root)."""
    return f"{stack['id']}@{stack['dir']}"


def scan(root: str | Path) -> Scan:
    """Every stack under ``root`` with its commands, plus a fingerprint of the marker files (paths, sizes, mtimes)."""
    root = Path(root)
    root_names = _names(root)
    stacks: list[dict[str, Any]] = []
    marker_paths: list[str] = []
    for rel in _scan_dirs(root, root_names):
        if rel != "." and not _SAFE_DIR.match(rel):
            continue  # a directory name that cannot be shown as a plain path is not put into prompts or rules
        d = root if rel == "." else root / rel
        names = root_names if rel == "." else _names(d)
        found: dict[str, list[str]] = {}
        for m in MARKERS:
            if m.root_only and rel != ".":
                continue
            hits = _matches(d, names, m)
            if hits:
                found.setdefault(m.stack, []).extend(hits)
        for sid, hits in found.items():
            manager, cmds, extra = DERIVE[sid](d, names)
            paths = sorted({h if rel == "." else f"{rel}/{h}" for h in hits})
            marker_paths += paths
            stacks.append(
                {
                    "id": sid,
                    "dir": rel,
                    "manager": manager,
                    "commands": {k: cmds[k] for k in COMMAND_KINDS if cmds.get(k)},
                    "markers": paths,
                    "extra": extra,
                }
            )
    marker_paths = sorted(set(marker_paths))
    return Scan(
        stacks=stacks,
        markers=marker_paths,
        fingerprint=fingerprint(root, marker_paths),
        workspaces=bool(_workspace_globs(root, root_names))
        or bool(root_names & {"pnpm-workspace.yaml", "lerna.json", "nx.json", "turbo.json", "go.work"}),
    )


def fingerprint(root: Path, marker_paths: list[str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(marker_paths):
        try:
            st = (root / rel).stat()
            h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        except OSError:
            h.update(f"{rel}\0-\n".encode())
    return h.hexdigest()[:16]


def legacy_info(s: Scan, root: Path) -> dict[str, Any]:
    """The single-language view older code reads (``language``, ``package_manager``, ``test``/``lint``/``build``...)."""
    info: dict[str, Any] = {
        "language": "unknown",
        "package_manager": "",
        "test": "",
        "lint": "",
        "build": "",
        "ci": "",
        "monorepo": False,
        "docker": any(x["id"] == "docker" for x in s.stacks),
        "makefile": (root / "Makefile").is_file(),
    }
    langs = [x for x in s.stacks if x["id"] in LANGUAGES]
    primary = next((x for x in langs if x["dir"] == "."), langs[0] if langs else None)
    if primary is not None:
        lang = primary["id"]
        if lang == "node":
            lang = "typescript" if primary["extra"].get("typescript") else "javascript"
        info.update(language=lang, package_manager=primary["manager"])
        for k in ("test", "lint", "build"):
            info[k] = primary["commands"].get(k, "")
    make = next((x for x in s.stacks if x["id"] == "make" and x["dir"] == "."), None)
    if make is not None and not info["test"]:
        for k in ("test", "lint", "build"):
            info[k] = make["commands"].get(k, "")
    ci = next((x for x in s.stacks if x["id"] == "ci"), None)
    info["ci"] = ci["manager"].split(",")[0] if ci else ""
    rust_ws = any(x["id"] == "rust" and x["extra"].get("workspace") for x in s.stacks)
    info["monorepo"] = s.workspaces or rust_ws or len({x["dir"] for x in langs}) > 1
    return info
