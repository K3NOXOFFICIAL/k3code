"""The twelve setup steps. Each takes a :class:`Ctx` and returns a JSON-serialisable dict (never secrets)."""

from __future__ import annotations

import copy
import os
import re
import shutil
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from k3code import context_budget, service
from k3code.config import env_value
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
#: What an answers file may set: the three of the menu and the other two the config accepts.
ANSWER_PERMISSION_MODES = [*PERMISSION_MODES, "plan", "auto"]
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
    only_step: str | None = None  # set for `setup --step X`: the one step that runs

    def say(self, text: str = "") -> None:
        self.p.say(text)

    @property
    def live(self) -> bool:
        """May setup call the network: no ``--no-probe``, and the providers step was not told not to test live."""
        return self.do_probe and bool(self.data.get("providers", {}).get("live", True))


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


def unique_name(name: str, taken: Collection[str]) -> str:
    """``name``, or ``name-2``, ``name-3``, ... when another provider entry already has it."""
    out, n = name, 2
    while out in taken:
        out, n = f"{name}-{n}", n + 1
    return out


def key_env_for(name: str, taken: Collection[str] = ()) -> str:
    """The key variable of an entry whose preset names none: ``K3CODE_<NAME>_API_KEY``, one not in ``taken``.

    One variable per endpoint: a shared K3CODE_API_KEY sent the key of one endpoint to the next one set up, and
    storing the second key overwrote the first (#114, #120)."""
    stem = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "ENDPOINT"
    var, n = f"K3CODE_{stem}_API_KEY", 2
    while var in taken:
        var, n = f"K3CODE_{stem}_{n}_API_KEY", n + 1
    return var


def key_env_at(base_url: str, name: str, before: Sequence[Any], taken: Collection[str] = ()) -> str:
    """The key variable of entry ``name`` at ``base_url``, which is not a vendor preset at its own URL.

    ``before`` is the provider chain in the config now. Its entry for the same URL passes its variable on, unless an
    entry for another URL or one in ``taken`` uses it, or it is a vendor preset's (that one holds the vendor's key).
    Otherwise a new one that holds no value yet: a leftover key belongs to some other endpoint (#114, #120)."""
    url = base_url.rstrip("/")
    entries = [e for e in before if isinstance(e, dict)]
    olds = [(str(e.get("base_url") or "").rstrip("/"), str(e.get("api_key_env") or "")) for e in entries]
    used = {*taken, *(var for at, var in olds if at != url), *(s["api_key_env"] for s in probe.PRESETS.values())}
    same = next((var for at, var in olds if at == url), "")
    if same and same not in used:
        return same
    return key_env_for(name, used | set(os.environ) | set(read_env_file()))


def _key_env(name: str, base_url: str, preset: dict[str, str], others: Sequence[dict[str, Any]]) -> str:
    """A vendor preset's variable only at that vendor's own URL: at another URL the vendor's key would go there, and
    that entry's key would replace the vendor's. Anything else: :func:`key_env_at` over the configured chain."""
    if preset.get("api_key_env") and base_url.rstrip("/") == preset["base_url"].rstrip("/"):
        return preset["api_key_env"]
    from k3code import confio
    from k3code.paths import user_config_path

    before = confio.read_yaml(user_config_path()).get("providers") or []
    return key_env_at(base_url, name, before, {str(e.get("api_key_env")) for e in others})


def _entry_from(
    c: Ctx, idx: int, label: str, spec: dict[str, Any] | None, others: Sequence[dict[str, Any]] = ()
) -> dict[str, Any] | None:
    """Build one provider entry (interactive: ask; answers: ``spec``). Secrets go to the env file only.

    ``others`` are the entries already built in this run: a name or key variable made up here differs from theirs."""
    names = {str(e.get("name")) for e in others}
    if spec is None:
        if not c.p.confirm(f"providers.add{idx}", f"Add a {label} provider?", idx == 0):
            return None
        preset = c.p.select(f"providers.preset{idx}", "Preset", PRESET_CHOICES, default_preset())
        if preset == "claude-cli":  # Claude Code's own login: no URL and no key
            return {"name": "claude-cli", "kind": "claude-cli"}
        spec = {"preset": preset, "name": unique_name(preset, names)}
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
        spec["api_key_env"] = _key_env(spec["name"], spec["base_url"], base, others)
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
    name = spec.get("name") or unique_name(spec.get("preset") or f"provider{idx + 1}", names)
    url = spec.get("base_url") or base.get("base_url", "")
    entry = {
        "name": name,
        "kind": spec.get("kind") or base.get("kind", "openai"),
        "base_url": url,
        "api_key_env": spec.get("api_key_env") or _key_env(name, url, base, others),
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
    from k3code import confio
    from k3code.paths import user_config_path

    entries = confio.read_yaml(user_config_path()).get("providers") or []
    chain = [str(p.get("name")) for p in entries if isinstance(p, dict)]
    if chain:
        c.say(f"  config.yaml has the provider chain {', '.join(chain)}; this step replaces it with the entries below.")
    labels = ["primary", "secondary", "tertiary"]
    specs = c.p.raw("providers.entries")
    entries: list[dict[str, Any]] = []
    for i, label in enumerate(labels):
        if specs is not None:
            if i >= len(specs):
                break
            e = _entry_from(c, i, label, dict(specs[i]), entries)
        else:
            e = _entry_from(c, i, label, None, entries)
            if e is None:
                break
        if e:
            entries.append(e)
    results: dict[str, Any] = {}
    test_live = c.p.confirm("providers.test", "Test each provider live?", c.p.interactive)  # kept for later steps
    if test_live and c.do_probe:
        for e in entries:
            if e.get("kind") == "claude-cli":  # no endpoint to list models from
                continue
            ok, ms, _ids, detail = probe.list_models(e, env_value(e["api_key_env"]))
            c.say(f"  {e['name']}: {'OK' if ok else 'FAIL'} {ms:.0f} ms ({detail})")
            results[e["name"]] = ok
    if entries and all(probe.is_gateway(e) or e["name"] in _GATEWAYS for e in entries):
        c.say("  WARNING: every entry goes through a self-hosted gateway; add a direct provider as a bypass.")
    elif not entries:
        c.say("  WARNING: no provider configured; k3code cannot run models.")
    return {"entries": entries, "tested": results, "live": test_live}


def _tier_answer(c: Ctx, tier: str, provider: str) -> Any:
    """The answers file's model for ``tier``: one id for every provider, or a mapping from provider name."""
    given = c.p.raw(f"tiers.{tier}")
    return given.get(provider) if isinstance(given, dict) else given


def step_tiers(c: Ctx) -> dict[str, Any]:
    c.say("Tiers: main = normal work, strong = hard problems, cheap = background/loop/cron, fast = quick replies.")
    providers = c.data.get("providers", {}).get("entries", [])
    out: dict[str, dict[str, str]] = {}
    for e in providers:
        claude_cli = e.get("kind") == "claude-cli"
        answers = {t: _tier_answer(c, t, e["name"]) for t in TIERS}
        # the model list is only there to choose a tier's model from: no live tests, or every tier answered, no call
        live = c.live and not claude_cli and any(a is None for a in answers.values())
        key = env_value(e.get("api_key_env", ""))  # the shell, then the env file the wizard writes
        ok, _ms, ids, _d = probe.list_models(e, key) if live else (False, 0, [], "")
        tiers: dict[str, str] = {}
        for t in TIERS:
            given = answers[t]
            if given is not None:
                tiers[t] = str(given)
            elif ok and ids and c.p.interactive:
                choices = ["(type one)", *ids[: probe.MAX_MODEL_CHOICES]]
                pick = c.p.select(f"tiers.{e['name']}.{t}", f"{e['name']}: model for '{t}'", choices, ids[0])
                tiers[t] = pick if pick != "(type one)" else c.p.text(f"tiers.{e['name']}.{t}.custom", "Model id")
            else:
                fallback = CLAUDE_CLI_TIERS.get(t, "") if claude_cli else ""
                tiers[t] = c.p.text(f"tiers.{e['name']}.{t}", f"{e['name']}: model for '{t}'", fallback)
        out[e["name"]] = {t: m for t, m in tiers.items() if m}
    windows = _context_windows(c, sorted({m for tiers in out.values() for m in tiers.values()}))
    return {"models": out, "context_windows": windows}


#: The largest window setup accepts: past this a typo ("1e12") would switch compaction off for good.
MAX_WINDOW = 10_000_000


def parse_window(text: str) -> int | None:
    """Tokens from ``32000``, ``32_000``, ``32,000`` or ``32k``; None for anything else (or a nonsense size)."""
    t = text.strip().lower().replace("_", "").replace(",", "")
    mult = 1000 if t.endswith("k") else 1
    try:
        n = int(float(t.removesuffix("k")) * mult)
    except (ValueError, OverflowError):  # "abc"; "inf" and "1e400"
        return None
    return n if 1000 <= n <= MAX_WINDOW else None


def _context_windows(c: Ctx, model_ids: list[str]) -> dict[str, int]:
    """The real context window of each chosen model id k3code has no figure for (a gateway alias, a small local model).

    Compaction keys to the window and an unknown id is assumed to have FALLBACK_WINDOW, so a smaller model only learns
    its limit from the provider's overflow error. Interactive: asked once per such id, blank keeps the assumption (and
    `k3code doctor` keeps warning). An answers file lists them as ``tiers.context_windows: {<model id>: <tokens>}``,
    for any id, tiers or not. Ids that already have a ``models.<id>.context_window`` in the user config are not asked
    again.
    """
    from types import SimpleNamespace

    from k3code import confio
    from k3code.paths import user_config_path

    given = c.p.raw("tiers.context_windows")
    given = given if isinstance(given, dict) else {}
    existing = SimpleNamespace(models=confio.read_yaml(user_config_path()).get("models") or {})
    out: dict[str, int] = {}
    for m in sorted({*model_ids, *map(str, given)}):
        value: Any = given.get(m)
        if value is None:
            known = context_budget.family_window(m) is not None or context_budget.has_explicit_window(existing, m)
            if known or not c.p.interactive:
                continue
            value = c.p.text(
                f"tiers.context_window.{m}",
                f"Context window of '{m}' in tokens, e.g. 32000 or 32k (unknown model; blank = assume "
                f"{context_budget.FALLBACK_WINDOW})",
                "",
            )
        if not str(value).strip():
            continue
        n = parse_window(str(value))
        if n is None:
            c.say(
                f"  Not a context window size: {value!r} (for {m}); keeping the {context_budget.FALLBACK_WINDOW} guess."
            )
            continue
        out[m] = n
    return out


def step_permissions(c: Ctx) -> dict[str, Any]:
    default = c.data.get("usage", {}).get("permission_mode", "ask")
    modes = PERMISSION_MODES if c.p.interactive else ANSWER_PERMISSION_MODES
    mode = c.p.select("permissions.mode", "Default permission mode", modes, default)
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
    # an empty answer is no entry: a blank pattern would match (and deny) every command
    return {"mode": mode, "extra_hardline": [str(x) for x in extra if str(x).strip()]}


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
        try:
            for line in service.install():
                c.say("  " + line)
        except service.ServiceError as e:  # nothing was written
            c.say(f"  {e}")
            out["install"] = False
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


#: the ``models`` keys of a provider entry that the tiers step writes (main is stored as ``default``)
_TIER_MODEL_KEYS = ("default", *(t for t in TIERS if t != "main"))


def _tier_models(tiers: dict[str, Any]) -> dict[str, Any]:
    models: dict[str, Any] = {}
    if "main" in tiers:
        models["default"] = tiers["main"]
    models.update({k: v for k, v in tiers.items() if k != "main"})
    return models


def _with_tier_models(old: Any, new: dict[str, Any]) -> dict[str, Any]:
    """The tier keys of an entry's ``models`` are replaced by ``new``; any other key of it is kept."""
    kept = {k: v for k, v in dict(old or {}).items() if k not in _TIER_MODEL_KEYS}
    return {**kept, **new}


def build_config(data: dict[str, Any]) -> dict[str, Any]:
    """Assemble ``config.yaml`` content from the collected step data. No secrets, only ``api_key_env`` names."""
    usage = data.get("usage", {})
    providers = []
    models_by = data.get("tiers", {}).get("models", {})
    for e in data.get("providers", {}).get("entries", []):
        providers.append({**e, "models": _tier_models(models_by.get(e["name"], {}))})
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
    # one line each, and nothing an answer says can close the block early
    return "\n".join(" ".join(ln.replace("-->", "").replace("<!--", "").split()) for ln in lines) + "\n"


#: config keys each step owns (single-step re-runs only touch these; a dotted path is a key inside a section)
OWNS: dict[str, tuple[str, ...]] = {
    "usage": ("output_style", "autonomy.plan_first", "autonomy.fanout.max_parallel"),
    "providers": ("providers",),
    "tiers": ("providers",),  # the tier keys of ``models`` of the entries already in config.yaml, nothing else
    "permissions": ("permission_mode", "permissions.hardline"),
    "integrations": ("mcp.servers", "skills.roots", "mem0.url"),  # research.searxng_url is handled in write_config
    "theme": ("display.theme", "display.focus_mode", "panes.keymap"),
}

USER_MD_BEGIN = "<!-- k3code:setup begin (k3code setup rewrites only the lines up to the end marker) -->"
USER_MD_END = "<!-- k3code:setup end -->"
_USER_MD_BLOCK = re.compile(r"^<!-- k3code:setup begin\b.*?-->\n.*?^<!-- k3code:setup end -->\n?", re.S | re.M)
_USER_MD_MARKER = re.compile(r"^<!-- k3code:setup (?:begin\b.*?|end) -->\n", re.M)
#: the lines the wizard wrote into USER.md before it had a block (an older file has them without markers)
_USER_MD_OLD_LINE = re.compile(
    r"^(?:- Name/handle: .*|- Language: .*; experience: .*; verbosity: .*|- Primary use: .*; languages: .*; "
    r"frameworks: .*|- System: .*, shell .*, editor .*, toolchains .*, tailscale (?:True|False))\n",
    re.M,
)


def write_user_md(data: dict[str, Any], say: Callable[[str], None] | None = None) -> Path:
    """Put the setup answers into USER.md inside a marked block; the user's own lines and the learned preferences
    around it are never touched. A USER.md from before the block existed is backed up (``USER.md.bak-<ts>``) and
    gets the block; its lines that setup itself wrote are replaced by the block, everything else stays."""
    from k3code import confio
    from k3code.memory import user_memory_path

    mem = user_memory_path()
    ensure_private_dir(mem.parent)  # user memory is private state: 0600/0700 whatever the umask
    block = f"{USER_MD_BEGIN}\n{user_md(data)}{USER_MD_END}\n"
    old = mem.read_text(encoding="utf-8") if mem.is_file() else None
    if old is None:
        new = "# About the user\n\n" + block
    elif m := _USER_MD_BLOCK.search(old):
        new = old[: m.start()] + block + old[m.end() :]
    else:
        if old.strip():
            bak = confio.backup_path(mem)
            private_file(bak).write_text(old, encoding="utf-8")
            if say:
                say(f"  {mem.name} had no setup block: saved a copy as {bak.name}; your own lines are kept.")
        rest = _USER_MD_OLD_LINE.sub("", _USER_MD_MARKER.sub("", old if old.endswith("\n") else old + "\n"))
        rest = re.sub(r"\A\s*# About the user[ \t]*\n", "", rest).strip("\n")
        new = "# About the user\n\n" + block + (f"\n{rest}\n" if rest else "")
    if new != old:
        private_file(mem).write_text(new, encoding="utf-8")
    return mem


_MISSING = object()


def _get_path(d: dict[str, Any], dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return _MISSING
        d = d[part]
    return d


def _set_path(d: dict[str, Any], dotted: str, value: Any) -> None:
    head, _, rest = dotted.partition(".")
    if not rest:
        d[head] = value
        return
    if not isinstance(d.get(head), dict):
        d[head] = {}
    _set_path(d[head], rest, value)


def _drop_path(d: dict[str, Any], dotted: str) -> None:
    """Remove the key; a section left empty by it goes too, but a section with other keys stays."""
    head, _, rest = dotted.partition(".")
    if not rest:
        d.pop(head, None)
    elif isinstance(d.get(head), dict):
        _drop_path(d[head], rest)
        if not d[head]:
            del d[head]


def write_config(data: dict[str, Any], only: str | None = None) -> Path:
    """Write ``config.yaml`` from the step data, merged into what is there. Only the keys the steps own (``OWNS``)
    are written, each one inside its section, so the rest of the file (display settings, autonomy keys, permission
    rules, ...) survives. With ``only``, just the keys that step owns; the provider chain comes from the file."""
    from k3code import confio
    from k3code.paths import user_config_path

    cfg = build_config(data)
    path = user_config_path()
    existing = confio.read_yaml(path)
    merged = copy.deepcopy(existing)
    for key in tuple(k for ks in OWNS.values() for k in ks) if only is None else OWNS.get(only, ()):
        if key == "providers":
            continue
        value = _get_path(cfg, key)
        if value is not _MISSING:
            _set_path(merged, key, value)
        elif only is not None:  # owned but now empty (e.g. all MCP servers removed): cleared, its section's rest stays
            _drop_path(merged, key)
    if only in (None, "tiers"):
        merged.setdefault("tiers", cfg["tiers"])  # the tier map is not asked about: a customised one stays
    old_chain = [p for p in existing.get("providers") or [] if isinstance(p, dict)]
    by_name = {p.get("name"): p for p in old_chain}
    if only == "tiers":
        # never rebuild the chain from the saved answers (they may be older than config.yaml, or absent after a fast
        # onboarding): only the tier models of the entries that are in the file change
        by_tier = (data.get("tiers") or {}).get("models") or {}
        merged["providers"] = [
            {**p, "models": _with_tier_models(p.get("models"), _tier_models(by_tier[p["name"]]))}
            if by_tier.get(p.get("name"))  # a blank answer for an entry keeps the models it has
            else p
            for p in old_chain
        ]
        if not old_chain:
            merged.pop("providers")
    elif only in (None, "providers") and cfg["providers"]:  # nothing collected (e.g. an imported bundle): no change
        keep_models = only == "providers" and "tiers" not in data  # models already configured stay
        chain = []
        for e in cfg["providers"]:
            old = by_name.get(e["name"], {})
            # no tier models collected for the entry (a summary-only run, a chain made elsewhere): the ones it has stay
            keep = keep_models or not e["models"]
            models = old.get("models", {}) if keep else _with_tier_models(old.get("models"), e["models"])
            chain.append({**old, **{k: v for k, v in e.items() if k != "models"}, "models": models})
        merged["providers"] = chain
    windows = (data.get("tiers") or {}).get("context_windows")
    if windows and only in (None, "tiers"):  # models.<id>.context_window: other keys of the id and other ids survive
        models = {k: dict(v) if isinstance(v, dict) else {} for k, v in dict(existing.get("models") or {}).items()}
        for model_id, n in windows.items():
            models[model_id] = {**models.get(model_id, {}), "context_window": int(n)}
        merged["models"] = models
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
    if merged != existing or not path.is_file():  # a re-run that changes nothing leaves the file alone
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
    from k3code.paths import user_config_path

    # `--step summary` owns no config keys: config.yaml is left as it is. The end of a full run writes it.
    path = user_config_path() if c.only_step else write_config(c.data)
    mem = write_user_md(c.data, c.say)
    c.say(f"Wrote {mem}" if c.only_step else f"Wrote {path} and {mem}")
    checks = asyncio.run(doctor.run_checks(load_config(), probe=c.do_probe, network=c.live))
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
#: Trees too slow to walk for a recipe scan: under WSL, ``/mnt/c`` goes through the Windows file system (8 s once).
SLOW_MOUNTS = (Path("/mnt"),)


def project_scan_skip_reason(cwd: Path) -> str:
    """Why fast setup must not scan ``cwd`` for project recipes; '' when it is a project worth scanning.

    A home directory (or a parent of it), a directory outside any git repository and a slow mount are not a project:
    scanning them takes seconds and offers recipes for whatever sits in the tree."""
    from k3code.learning import projectstate

    root = projectstate.project_root(cwd)
    home_dir = Path.home().resolve()
    if root == home_dir or root in home_dir.parents:
        return "it is your home directory or a parent of it"
    for mount in SLOW_MOUNTS:
        mount = mount.resolve()
        if root == mount or mount in root.parents:
            return f"it is under {mount}"
    if not (root / ".git").exists():
        return "it is not inside a git repository"
    return ""


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
