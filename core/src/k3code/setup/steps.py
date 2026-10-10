"""The twelve setup steps. Each takes a :class:`Ctx` and returns a JSON-serialisable dict (never secrets)."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from k3code import service
from k3code.gateway.sessions import SessionStore
from k3code.outputstyle import PRESETS as STYLE_PRESETS
from k3code.paths import ensure_private_dir, home, private_file
from k3code.permissions.hardline import HARDLINE_NAMES
from k3code.setup import detect, probe
from k3code.setup.prompter import Prompter
from k3code.setup.state import env_file_path, read_env_file, set_env_var

if TYPE_CHECKING:
    from k3code.bundle import SensitiveItem

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TIERS = ("main", "strong", "cheap", "fast")
PERMISSION_MODES = ["ask", "auto-edit", "yolo"]
USES = ["coding", "ops", "research", "mixed"]
#: use → (permission mode, plan-first, fan-out cap, output style)
USE_DEFAULTS: dict[str, tuple[str, bool, int, str]] = {
    "coding": ("auto-edit", True, 4, "default"),
    "ops": ("ask", True, 2, "concise"),
    "research": ("ask", False, 6, "explanatory"),
    "mixed": ("ask", True, 3, "default"),
}
_GATEWAYS: set[str] = set()  # names of entries created from the self-hosted gateway preset
BASE_THEMES = ["default", "midnight", "light", "solarized", "mono"]
#: Provider choices in the wizard. "claude-cli" is Claude Code's own login (no URL, no key). "custom" is any
#: OpenAI-compatible endpoint, typed in by the user. The other presets are vendor endpoints.
PRESET_CHOICES = ["claude-cli", *probe.PRESETS, "custom"]
#: Model aliases for Claude Code's own login, offered as defaults for the four tiers.
CLAUDE_CLI_TIERS = {"main": "sonnet", "strong": "opus", "cheap": "haiku", "fast": "haiku"}


def default_preset() -> str:
    """The preselected provider: Claude Code's own login when `claude` is installed, else a custom endpoint."""
    return "claude-cli" if shutil.which("claude") else "custom"


@dataclass
class Ctx:
    p: Prompter
    data: dict[str, Any]  # results of previous steps
    do_probe: bool = True
    cwd: Path = field(default_factory=Path.cwd)

    def say(self, text: str = "") -> None:
        self.p.say(text)


def themes() -> list[str]:
    extra = sorted(f.stem for f in (home() / "skins").glob("*.y*ml")) if (home() / "skins").is_dir() else []
    return [*BASE_THEMES, *[t for t in extra if t not in BASE_THEMES]]


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def step_welcome(c: Ctx) -> dict[str, Any]:
    c.say("Welcome to k3code setup. It is resumable: Ctrl+C any time and re-run `k3code setup`.")
    mode = c.p.select("welcome.mode", "Fresh setup or import a .k3bundle?", ["fresh", "import"], "fresh")
    out: dict[str, Any] = {"mode": mode}
    if mode == "import":
        path = c.p.text("welcome.bundle", "Path to the .k3bundle")
        out["bundle"] = path
        if path:
            c.say("Imported:\n" + apply_import(path, c.cwd, accept=lambda item: _accept_item(c.p, item)))
            c.say("Only secrets are missing: they are asked for in the providers step.")
    return out


def _accept_item(p: Prompter, item: SensitiveItem) -> bool:
    """One explicit yes per MCP server, permission rule, provider endpoint or hook in the bundle; the default is no.
    An answers file says yes with ``welcome.accept.<item key>: true`` (nested: the key's dots are levels)."""
    return p.confirm(f"welcome.accept.{item.key}", f"{item.text()}\nApply {item.key}?", False)


def apply_import(bundle_path: str, cwd: Path, accept: Callable[[SensitiveItem], bool] | None = None) -> str:
    """Import settings and sessions from a bundle with the existing importer; returns its report. Sensitive user
    settings are applied only where ``accept`` says yes (see k3code.bundle.sensitive_items)."""
    from k3code.bundle import apply_bundle, read_bundle

    bundle = read_bundle(Path(bundle_path).expanduser())
    store = SessionStore(home() / "sessions.db")
    try:
        return apply_bundle(bundle, store=store, cwd=cwd, accept=accept).describe()
    finally:
        store.close()


def step_about(c: Ctx) -> dict[str, Any]:
    det = detect.detect()
    return {
        "name": c.p.text("about.name", "Your name or handle", det["git_name"]),
        "language": c.p.select("about.language", "Preferred language", ["en", "de"], "en"),
        "experience": c.p.select(
            "about.experience", "Experience level", ["beginner", "intermediate", "expert"], "expert"
        ),
        "verbosity": c.p.select(
            "about.verbosity", "How verbose should k3code be?", ["terse", "normal", "detailed"], "normal"
        ),
    }


def step_system(c: Ctx) -> dict[str, Any]:
    det = detect.detect(probe=c.do_probe)
    c.say("Detected: " + ", ".join(f"{k}={v}" for k, v in det.items() if v not in ("", [], False)))
    if c.p.confirm("system.confirm", "Is this correct?", True):
        out = dict(det)
    else:
        out = dict(det)
        out["shell"] = c.p.text("system.shell", "Shell", det["shell"])
        out["editor"] = c.p.text("system.editor", "Editor", det["editor"])
        out["terminal"] = c.p.text("system.terminal", "Terminal", det["terminal"])
        out["toolchains"] = _csv(
            c.p.text("system.toolchains", "Toolchains (comma separated)", ",".join(det["toolchains"]))
        )
        out["tailscale"] = c.p.confirm("system.tailscale", "Tailscale present?", det["tailscale"])
    # explicit overrides in an answers file always win
    for k in ("os", "shell", "terminal", "editor", "git_name", "git_email", "toolchains", "tailscale"):
        v = c.p.raw(f"system.{k}")
        if v is not None:
            out[k] = v
    return out


def step_usage(c: Ctx) -> dict[str, Any]:
    primary = c.p.select("usage.primary", "What will you mainly use k3code for?", USES, "mixed")
    out: dict[str, Any] = {"primary": primary, "languages": [], "frameworks": []}
    if primary in ("coding", "mixed"):
        langs = c.p.raw("usage.languages")
        out["languages"] = (
            langs if langs is not None else _csv(c.p.text("usage.languages", "Languages (comma separated)"))
        )
        fw = c.p.raw("usage.frameworks")
        out["frameworks"] = fw if fw is not None else _csv(c.p.text("usage.frameworks", "Frameworks (comma separated)"))
    mode, plan, fan, style = USE_DEFAULTS[primary]
    out.update(permission_mode=mode, plan_first=plan, fanout_cap=fan, output_style=style)
    c.say(f"Defaults for '{primary}': permission={mode}, plan-first={plan}, fan-out cap={fan}, style={style}")
    return out


def _entry_from(c: Ctx, idx: int, label: str, spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build one provider entry (interactive: ask; answers: ``spec``). Secrets go to the env file only."""
    if spec is None:
        if not c.p.confirm(f"providers.add{idx}", f"Add a {label} provider?", idx == 0):
            return None
        preset = c.p.select(f"providers.preset{idx}", "Preset", PRESET_CHOICES, default_preset())
        if preset == "claude-cli":  # Claude Code's own login: no URL and no key
            return {"name": "claude-cli", "kind": "claude-cli"}
        spec = {"preset": preset}
        base = probe.PRESETS.get(preset, {})
        spec["base_url"] = c.p.text(
            f"providers.base_url{idx}",
            "Base URL (OpenAI-compatible, e.g. https://host/v1)" if preset == "custom" else "Base URL",
            base.get("base_url", ""),
        ).strip()
        if not spec["base_url"]:
            c.say("  no base URL given: this provider is skipped")
            return None
        # Ask for the key itself first: pasting it is what most people mean. It is stored in the
        # 0600 env file under the preset's variable name; the config only ever names the variable.
        key = c.p.text(
            f"providers.key{idx}",
            f"API key for {preset} (paste it; hidden, stored 0600 in {env_file_path()}). "
            "Leave empty to use an environment variable",
            "",
            secret=True,
        )
        spec["api_key_env"] = base.get("api_key_env", "K3CODE_API_KEY")
        if key.strip():
            spec["api_key"] = key.strip()
        else:
            while True:
                name = c.p.text(
                    f"providers.env{idx}",
                    "Name of the environment variable that holds the key (letters, digits, _; e.g. K3CODE_API_KEY)",
                    spec["api_key_env"],
                ).strip()
                if _ENV_NAME.match(name):
                    spec["api_key_env"] = name
                    break
                c.say(
                    f"  '{name}' is not a valid environment variable name (no '-' or spaces). "
                    "Enter the variable NAME, not the key."
                )
            if not os.environ.get(spec["api_key_env"]) and not read_env_file().get(spec["api_key_env"]):
                c.say(f"  NOTE: {spec['api_key_env']} is not set yet. Export it, or add it to {env_file_path()}.")
    if spec.get("preset") == "claude-cli" or spec.get("kind") == "claude-cli":  # an answers file can name it too
        return {"name": spec.get("name") or "claude-cli", "kind": "claude-cli"}
    base = probe.PRESETS.get(spec.get("preset", ""), {})
    entry = {
        "name": spec.get("name") or spec.get("preset") or f"provider{idx + 1}",
        "kind": spec.get("kind") or base.get("kind", "openai"),
        "base_url": spec.get("base_url") or base.get("base_url", ""),
        "api_key_env": spec.get("api_key_env") or base.get("api_key_env", "K3CODE_API_KEY"),
    }
    if spec.get("preset") == "omniroute":
        _GATEWAYS.add(entry["name"])
    key = spec.get("api_key")
    if key:
        set_env_var(entry["api_key_env"], str(key))
        os.environ[entry["api_key_env"]] = str(key)
        c.say(f"  stored {entry['api_key_env']} in {env_file_path()} (0600)")
    return entry


def step_providers(c: Ctx) -> dict[str, Any]:
    labels = ["primary", "secondary", "tertiary"]
    specs = c.p.raw("providers.entries")
    entries: list[dict[str, Any]] = []
    for i, label in enumerate(labels):
        if specs is not None:
            if i >= len(specs):
                break
            e = _entry_from(c, i, label, dict(specs[i]))
        else:
            e = _entry_from(c, i, label, None)
            if e is None:
                break
        if e:
            entries.append(e)
    results: dict[str, Any] = {}
    if c.p.confirm("providers.test", "Test each provider live?", c.p.interactive) and c.do_probe:
        for e in entries:
            if e.get("kind") == "claude-cli":  # no endpoint to list models from
                continue
            ok, ms, _ids, detail = probe.list_models(e, os.environ.get(e["api_key_env"], ""))
            c.say(f"  {e['name']}: {'OK' if ok else 'FAIL'} {ms:.0f} ms ({detail})")
            results[e["name"]] = ok
    if entries and all(probe.is_gateway(e) or e["name"] in _GATEWAYS for e in entries):
        c.say("  WARNING: every entry goes through a self-hosted gateway; add a direct provider as a bypass.")
    elif not entries:
        c.say("  WARNING: no provider configured; k3code cannot run models.")
    return {"entries": entries, "tested": results}


def step_tiers(c: Ctx) -> dict[str, Any]:
    c.say("Tiers: main = normal work, strong = hard problems, cheap = background/loop/cron, fast = quick replies.")
    providers = c.data.get("providers", {}).get("entries", [])
    out: dict[str, dict[str, str]] = {}
    for e in providers:
        claude_cli = e.get("kind") == "claude-cli"
        live = c.do_probe and not claude_cli
        ok, _ms, ids, _d = probe.list_models(e, os.environ.get(e["api_key_env"], "")) if live else (False, 0, [], "")
        tiers: dict[str, str] = {}
        for t in TIERS:
            given = c.p.raw(f"tiers.{t}")
            if isinstance(given, dict):
                given = given.get(e["name"])
            if given is not None:
                tiers[t] = str(given)
            elif ok and ids and c.p.interactive:
                pick = c.p.select(f"tiers.{e['name']}.{t}", f"{e['name']}: model for '{t}'", [*ids, "(type one)"])
                tiers[t] = pick if pick != "(type one)" else c.p.text(f"tiers.{e['name']}.{t}.custom", "Model id")
            else:
                fallback = CLAUDE_CLI_TIERS.get(t, "") if claude_cli else ""
                tiers[t] = c.p.text(f"tiers.{e['name']}.{t}", f"{e['name']}: model for '{t}'", fallback)
        out[e["name"]] = {t: m for t, m in tiers.items() if m}
    return {"models": out}


def step_permissions(c: Ctx) -> dict[str, Any]:
    default = c.data.get("usage", {}).get("permission_mode", "ask")
    mode = c.p.select("permissions.mode", "Default permission mode", PERMISSION_MODES, default)
    c.say("Hardline denies (never allowed, even in yolo):")
    for name in HARDLINE_NAMES:
        c.say(f"  - {name}")
    extra = c.p.raw("permissions.extra_hardline")
    if extra is None:
        extra = []
        while c.p.interactive and c.p.confirm(
            "permissions.more", "Add an extra hardline regex (e.g. 'ssh \\S+ systemctl')?", False
        ):
            extra.append(c.p.text("permissions.rx", "Regex"))
    return {"mode": mode, "extra_hardline": [str(x) for x in extra]}


def step_integrations(c: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {"mcp": {}, "mem0_url": "", "skills_roots": [], "searxng_url": ""}
    mcp = c.p.raw("integrations.mcp")
    if mcp is None:
        mcp = []
        while c.p.interactive and c.p.confirm("integrations.addmcp", "Add an MCP server?", False):
            name = c.p.text("integrations.mcp_name", "Name")
            target = c.p.text("integrations.mcp_target", "URL (http…) or stdio command")
            mcp.append({"name": name, "url" if target.startswith("http") else "command": target})
    for s in mcp:
        s = dict(s)
        name = s.pop("name")
        if "command" in s and isinstance(s["command"], str) and " " in s["command"]:
            parts = s["command"].split()
            s["command"], s["args"] = parts[0], parts[1:]
        out["mcp"][name] = s
    out["mem0_url"] = c.p.text("integrations.mem0_url", "mem0 URL (blank = none)", "")
    roots = c.p.raw("integrations.skills_roots")
    out["skills_roots"] = (
        roots
        if roots is not None
        else _csv(c.p.text("integrations.skills_roots", "Skills roots (comma separated)", ""))
    )
    out["searxng_url"] = c.p.text("integrations.searxng_url", "SearXNG URL (blank = none)", "")
    if c.do_probe:
        for label, url in (("mem0", out["mem0_url"]), ("searxng", out["searxng_url"])):
            if url and c.p.confirm(f"integrations.test_{label}", f"Test {label} now?", True):
                ok, ms, detail = probe.probe_url(url)
                c.say(f"  {label}: {'OK' if ok else 'FAIL'} {ms:.0f} ms ({detail})")
    return out


def step_theme(c: Ctx) -> dict[str, Any]:
    names = themes()
    theme = c.p.select("theme.theme", "Default theme", names, names[0])
    if c.p.interactive:
        c.say(
            f"Preview ({theme}): \x1b[1mbold\x1b[0m \x1b[36mcyan\x1b[0m \x1b[32mgreen\x1b[0m "
            "\x1b[33myellow\x1b[0m \x1b[31mred\x1b[0m"
        )
    return {
        "theme": theme,
        "focus_mode": c.p.confirm("theme.focus_mode", "Focus mode on by default?", False),
        "keymap": c.p.select("theme.keymap", "Keymap style for k3 panes", ["k3", "tuios"], "k3"),
    }


def step_service(c: Ctx) -> dict[str, Any]:
    want = c.p.confirm("service.install", "Install the 24/7 systemd user service now?", False)
    out: dict[str, Any] = {"install": want}
    if want:
        for line in service.install():
            c.say("  " + line)
    c.say("To keep it running after logout: `loginctl enable-linger $USER` (needs sudo; run it yourself).")
    return out


TOUR = [
    "TUI:  Shift+Tab cycles permission mode · Ctrl+F toggles focus mode · ← (empty input) opens the agent view",
    "TUI:  ← opens the agent view · /commands lists every slash command (/settings, /update, /doctor)",
    "TUI:  /settings shows config; re-run any setup step with `k3code setup --step <name>`",
    "Panes (k3): Ctrl+G enters the pane prefix mode · Esc leaves it",
    "Panes (k3): Alt+arrows move focus between panes",
    "Panes (k3): keymap style is set in setup step 'theme' (k3 | tuios)",
]


def step_tour(c: Ctx) -> dict[str, Any]:
    for line in TOUR:
        c.say("  " + line)
    return {"shown": True}


def build_config(data: dict[str, Any]) -> dict[str, Any]:
    """Assemble ``config.yaml`` content from the collected step data. No secrets, only ``api_key_env`` names."""
    usage = data.get("usage", {})
    providers = []
    models_by = data.get("tiers", {}).get("models", {})
    for e in data.get("providers", {}).get("entries", []):
        t = models_by.get(e["name"], {})
        models: dict[str, Any] = {}
        if "main" in t:
            models["default"] = t["main"]
        models.update({k: v for k, v in t.items() if k != "main"})
        providers.append({**e, "models": models})
    perms = data.get("permissions", {})
    integ = data.get("integrations", {})
    theme = data.get("theme", {})
    cfg: dict[str, Any] = {
        "providers": providers,
        "permission_mode": perms.get("mode", usage.get("permission_mode", "ask")),
        "output_style": usage.get("output_style", "default"),
        "display": {"theme": theme.get("theme", ""), "focus_mode": bool(theme.get("focus_mode", False))},
        "tiers": {"main": "default", "strong": "strong", "cheap": "cheap", "fast": "fast", "background": "cheap"},
        "autonomy": {
            "plan_first": bool(usage.get("plan_first", False)),
            "fanout": {"max_parallel": int(usage.get("fanout_cap", 3))},
        },
        "panes": {"keymap": theme.get("keymap", "k3")},
    }
    if perms.get("extra_hardline"):
        cfg["permissions"] = {"hardline": list(perms["extra_hardline"])}
    if integ.get("mcp"):
        cfg["mcp"] = {"servers": integ["mcp"]}
    if integ.get("skills_roots"):
        cfg["skills"] = {"roots": list(integ["skills_roots"])}
    if integ.get("mem0_url"):
        cfg["mem0"] = {"url": integ["mem0_url"]}
    if integ.get("searxng_url"):
        # research.searxng_url is the key the research tools read (a top-level "searxng" was silently dropped)
        cfg["research"] = {"searxng_url": integ["searxng_url"]}
    return cfg


def user_md(data: dict[str, Any]) -> str:
    a, s, u = data.get("about", {}), data.get("system", {}), data.get("usage", {})
    lines = [
        f"- Name/handle: {a.get('name', '')}",
        f"- Language: {a.get('language', 'en')}; experience: {a.get('experience', '')}; "
        f"verbosity: {a.get('verbosity', '')}",
        f"- Primary use: {u.get('primary', '')}; languages: {', '.join(u.get('languages', []))}; "
        f"frameworks: {', '.join(u.get('frameworks', []))}",
        f"- System: {s.get('os', '')}, shell {s.get('shell', '')}, editor {s.get('editor', '')}, "
        f"toolchains {', '.join(s.get('toolchains', []))}, tailscale {s.get('tailscale', False)}",
    ]
    return "\n".join(lines) + "\n"


#: config keys each step owns (single-step re-runs only touch these)
OWNS: dict[str, tuple[str, ...]] = {
    "usage": ("output_style", "autonomy"),
    "providers": ("providers",),
    "tiers": ("providers", "tiers"),
    "permissions": ("permission_mode", "permissions"),
    "integrations": ("mcp", "skills", "mem0", "research"),
    "theme": ("display", "panes"),
}


def write_user_md(data: dict[str, Any]) -> Path:
    from k3code.memory import user_memory_path

    mem = user_memory_path()
    ensure_private_dir(mem.parent)  # user memory is private state: 0600/0700 whatever the umask
    private_file(mem).write_text("# About the user\n\n" + user_md(data), encoding="utf-8")
    return mem


def write_config(data: dict[str, Any], only: str | None = None) -> Path:
    """Write ``config.yaml`` from the step data. With ``only``, change just the keys that step owns."""
    from k3code import confio
    from k3code.paths import user_config_path

    cfg = build_config(data)
    path = user_config_path()
    existing = confio.read_yaml(path)
    if only is not None:
        keys = OWNS.get(only, ())
        cfg = {k: v for k, v in cfg.items() if k in keys}
        if "providers" in cfg and "tiers" not in data:  # keep the models already configured
            old = {p["name"]: p.get("models", {}) for p in existing.get("providers", [])}
            cfg["providers"] = [{**p, "models": old.get(p["name"], {})} for p in cfg["providers"]]
        # a key owned by the step but now empty (e.g. all MCP servers removed) is cleared
        merged = {k: v for k, v in existing.items() if k not in keys or k in cfg}
    else:
        if not cfg["providers"]:  # e.g. imported bundle already carries them
            cfg.pop("providers")
        merged = existing
    merged = {**merged, **cfg}
    if only in (None, "integrations") and "integrations" in data:
        # the integrations answer owns searxng_url only: the user's other research.* keys (max_subquestions, ...)
        # survive a re-run, and a blank answer clears the URL. The legacy top-level searxng key is migrated away.
        research = {k: v for k, v in dict(existing.get("research") or {}).items() if k != "searxng_url"}
        if url := (data.get("integrations") or {}).get("searxng_url"):
            research["searxng_url"] = url
        merged.pop("searxng", None)
        if research:
            merged["research"] = research
        else:
            merged.pop("research", None)
    confio.validate(merged)
    confio.write_yaml(path, merged)
    if only in (None, "providers"):
        # A provider was just (re)written, usually with a new key: an auth cooldown armed for the old one must
        # not keep the entry out of the chain.
        from k3code.daemon import k3_home
        from k3code.router.cooldown import clear_auth_cooldowns

        clear_auth_cooldowns(k3_home() / "cooldowns.json")
    return path


def step_summary(c: Ctx) -> dict[str, Any]:
    import asyncio

    from k3code import doctor
    from k3code.config import load_config

    path = write_config(c.data)
    mem = write_user_md(c.data)
    c.say(f"Wrote {path} and {mem}")
    checks = asyncio.run(doctor.run_checks(load_config(), probe=c.do_probe))
    c.say(doctor.format_report(checks))
    return {"config": str(path)}


def step_secrets(c: Ctx) -> dict[str, Any]:
    """Ask only for API keys that the (e.g. imported) config references but the env file lacks."""
    from k3code import confio
    from k3code.paths import user_config_path
    from k3code.setup.state import read_env_file

    have = read_env_file()
    asked: list[str] = []
    for prov in confio.read_yaml(user_config_path()).get("providers", []):
        name = prov.get("api_key_env", "")
        if not name or name in have or os.environ.get(name):
            continue
        value = c.p.raw(f"secrets.{name}")
        if value is None:
            value = c.p.text(
                f"secrets.{name}", f"{name} (for provider {prov.get('name', '?')}; blank = skip)", secret=True
            )
        if value:
            set_env_var(name, str(value))
            os.environ[name] = str(value)
            asked.append(name)
    c.say(f"Stored {len(asked)} secret(s) in {env_file_path()} (0600)." if asked else "No missing secrets.")
    return {"stored": asked}


PROJECT_RECIPE_MODES = ("ask", "accept_all", "none")


def offer_project_recipes(p: Prompter, cwd: Path) -> dict[str, Any]:
    """Scan the project ``cwd`` is in and offer its recipe proposals (k3code.learning.recipes).

    Answers file: ``project_recipes: accept_all | none | ask``; ``ask`` without a terminal means ``none``. The project
    is always scanned (read-only) and its stacks and commands shown; ``none`` creates no proposal and stores nothing
    (a session still offers them as cards later)."""
    import asyncio

    from k3code.autonomy.proposals import ProposalStore
    from k3code.learning import projectprep, projectstate, recipes, stacks
    from k3code.paths import home

    mode = str(p.raw("project_recipes", "ask") or "ask")
    if mode not in PROJECT_RECIPE_MODES:
        raise ValueError(f"project_recipes must be one of: {', '.join(PROJECT_RECIPE_MODES)} (got {mode!r})")
    if mode == "ask" and not p.interactive:
        mode = "none"
    root = projectstate.project_root(cwd)
    if mode == "none":
        found = stacks.scan(root).stacks
        if not found:
            p.say(f"No stacks detected in {root}.")
            return {"recipes": "none", "accepted": []}
        p.say(f"Project {root}: " + ", ".join(recipes.label(s) for s in found))
        for s in found:
            cmds = ", ".join(f"{kind} `{cmd}`" for kind, cmd in (s.get("commands") or {}).items())
            if cmds:
                p.say(f"  {recipes.label(s)}: {cmds}")
        p.say("Recipes not offered (project_recipes: none); a session offers them as cards, or run /project there.")
        return {"recipes": "none", "accepted": []}
    store = ProposalStore(home())
    asyncio.run(projectprep.prepare(root, store=store))
    found = projectstate.load(root).get("stacks") or []
    if not found:
        return {"recipes": mode, "accepted": []}
    p.say(f"Project {root}: " + ", ".join(recipes.label(s) for s in found))
    accepted: list[str] = []
    for prop in projectprep.pending_recipes(store, root):
        if mode == "ask" and not p.confirm(f"project_recipes.{prop.id}", f"{prop.text} Apply now?", False):
            continue  # stays pending: offered again as a card in a session
        store.set_status(prop.id, "accepted")
        p.say(f"  {recipes.apply(prop.payload)}")
        accepted.append(prop.id)
    return {"recipes": mode, "accepted": accepted}


def step_project(c: Ctx) -> dict[str, Any]:
    return offer_project_recipes(c.p, c.cwd)


@dataclass
class Step:
    name: str
    fn: Callable[[Ctx], dict[str, Any]]
    title: str


STEPS: list[Step] = [
    Step("welcome", step_welcome, "Welcome and import"),
    Step("about", step_about, "About you"),
    Step("system", step_system, "System and environment"),
    Step("usage", step_usage, "Primary use"),
    Step("providers", step_providers, "Providers and fallback chain"),
    Step("tiers", step_tiers, "Model tiers and degradation"),
    Step("permissions", step_permissions, "Permissions"),
    Step("integrations", step_integrations, "Integrations"),
    Step("theme", step_theme, "Theme and UI"),
    Step("service", step_service, "24/7 service"),
    Step("tour", step_tour, "Keymap tour"),
    Step("project", step_project, "This project"),
    Step("summary", step_summary, "Summary"),
]
STEP_NAMES = [s.name for s in STEPS]
__all__ = [
    "STEPS",
    "STEP_NAMES",
    "Ctx",
    "build_config",
    "write_config",
    "write_user_md",
    "step_secrets",
    "apply_import",
    "STYLE_PRESETS",
]
