"""Onboarding: ``k3code onboard`` and the one-time first-run question on a plain interactive launch.

Fast = one API endpoint with its key, or the local claude-cli login, with safe defaults. Full = the setup wizard.
Nothing starts the wizard on its own: a plain launch only asks the question, once, while no config exists.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from k3code.paths import home, user_config_path
from k3code.setup import probe
from k3code.setup.prompter import Prompter
from k3code.setup.state import env_file_path, read_env_file
from k3code.setup.steps import Ctx, _entry_from, offer_project_recipes, write_config
from k3code.setup.wizard import run_setup

QUESTION = "Fast setup (API endpoint + key) or full setup?"
MODES = ["fast", "full"]
CLAUDE_CLI_MODEL = "sonnet"  # a Claude Code alias: resolves to the current Sonnet
NO_CONFIG_HINT = "No k3code provider configured. Run `k3code onboard` to set one up."


def marker_path() -> Path:
    """Remembers that the first-run question was answered (``$K3CODE_HOME/onboarding.json``)."""
    return home() / "onboarding.json"


def question_answered() -> bool:
    try:
        return bool(json.loads(marker_path().read_text(encoding="utf-8")).get("answer"))
    except (OSError, ValueError, AttributeError):
        return False


def record_answer(answer: str) -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"answer": answer}) + "\n", encoding="utf-8")


# Fast-setup providers, best first. "api" is any OpenAI-compatible endpoint (asks for the URL).
PROVIDERS = ["anthropic", "openai", "openrouter", "claude-cli", "local", "api"]
PROVIDER_HELP = {
    "anthropic": "Anthropic API key",
    "openai": "OpenAI API key",
    "openrouter": "OpenRouter key (many models)",
    "claude-cli": "your Claude Code login, no key",
    "local": "Ollama on this machine, no key",
    "api": "any OpenAI-compatible endpoint",
}
# Known-good tier models per provider; anything else is picked from the endpoint's own list.
TIER_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "main": "claude-sonnet-5-5",
        "strong": "claude-opus-5-5",
        "cheap": "claude-haiku-5-5",
        "fast": "claude-haiku-5-5",
    },
    "claude-cli": {"main": CLAUDE_CLI_MODEL, "strong": "opus", "cheap": "haiku", "fast": "haiku"},
}
MAX_MODEL_CHOICES = 15


def detect_providers() -> dict[str, str]:
    """What is already usable here: provider -> why (a key in the environment, the claude command, Ollama)."""
    found: dict[str, str] = {}
    env_file = read_env_file()
    for name in ("anthropic", "openai", "openrouter"):
        var = probe.PRESETS[name]["api_key_env"]
        if os.environ.get(var) or env_file.get(var):
            found[name] = f"{var} is set"
    if shutil.which("claude"):
        found["claude-cli"] = "the claude command is installed"
    ok, _ms, _detail = probe.probe_url(probe.PRESETS["local"]["base_url"].rsplit("/v1", 1)[0], timeout=0.4)
    if ok:
        found["local"] = "Ollama is running"
    return found


def _default_provider(found: dict[str, str]) -> str:
    """The first provider already set up here. With none: Claude Code's login if `claude` exists, else custom."""
    return next((n for n in PROVIDERS if n in found), "claude-cli" if shutil.which("claude") else "api")


def _pick_model(p: Prompter, name: str, ids: list[str]) -> str:
    """The provider's known main model when listed (or when nothing is listed), else a choice from the list."""
    known = TIER_MODELS.get(name, {}).get("main", "")
    if known and (not ids or known in ids):
        return known
    if not ids:
        return p.text("onboard.model", "Model id for everyday work (blank = set it later)", "").strip()
    if not p.interactive:
        return str(p.raw("onboard.model", ids[0]))
    shown = ids[:MAX_MODEL_CHOICES]
    pick = p.select("onboard.model", f"Model for everyday work ({len(ids)} available)", [*shown, "(type one)"])
    if pick == "(type one)":
        return p.text("onboard.model.custom", "Model id", "").strip()
    return pick


def _api_entry(p: Prompter, name: str, *, do_probe: bool) -> tuple[dict[str, Any], str]:
    """Build the provider entry for a preset (or "api": ask the URL) and choose its main model ("" if unknown)."""
    if name == "api":
        base = ""
        while not base:
            base = p.text("onboard.endpoint", "API endpoint (base URL, e.g. https://host/v1)", "").strip()
            if not base and not p.interactive:
                raise ValueError("onboard.endpoint is required for provider api")
        base = base.rstrip("/")
        preset = next((n for n, s in probe.PRESETS.items() if s["base_url"].rstrip("/") == base), "custom")
    else:
        preset = name
        base = probe.PRESETS[name]["base_url"]
    known = probe.PRESETS.get(preset, {})
    spec: dict[str, Any] = {
        "preset": preset,
        "name": preset if preset != "custom" else "endpoint",
        "base_url": base,
        "kind": known.get("kind", "openai"),
        "api_key_env": known.get("api_key_env", "K3CODE_API_KEY"),
    }
    env_name = spec["api_key_env"]
    existing = os.environ.get(env_name) or read_env_file().get(env_name, "")
    key = str(p.raw("onboard.key", "") or "").strip() if not p.interactive else ""
    if existing and not key:
        p.say(f"  using {env_name} from your environment")
    elif preset != "local" and not key:
        key = p.text(
            "onboard.key",
            f"API key (hidden; stored 0600 in {env_file_path()}; blank = set {env_name} yourself)",
            "",
            secret=True,
        ).strip()
    if key:
        spec["api_key"] = key
    entry = _entry_from(Ctx(p=p, data={}), 0, "primary", spec)
    if entry is None:  # only the interactive branch of _entry_from returns None
        raise ValueError("no provider entry was built")
    if not key and not existing and preset != "local":
        p.say(f"  NOTE: {env_name} is not set yet. Export it, or add it to {env_file_path()}.")

    ids: list[str] = []
    answered = p.raw("onboard.model", None) if not p.interactive else None
    if do_probe and not answered:
        ok, _ms, ids, detail = probe.list_models(entry, key or existing or os.environ.get(env_name, ""))
        if not ok:
            p.say(f"  could not list models ({detail}); check the key and URL with `k3code doctor`")
            ids = []
    model = str(answered).strip() if answered else _pick_model(p, preset, ids)
    if model:
        p.say(f"  model: {model}")
    return entry, model


def run_fast(p: Prompter, *, do_probe: bool = True) -> None:
    """Fast setup: one provider with defaults for everything else. Picks what is already set up here first."""
    found = detect_providers() if p.interactive else {}
    for name, why in found.items():
        p.say(f"  found {name}: {why}")
    labels = {n: f"{n:<11} {PROVIDER_HELP[n]}" + ("  (found)" if n in found else "") for n in PROVIDERS}
    picked = p.select(
        "onboard.provider", "Which model provider?", [labels[n] for n in PROVIDERS], labels[_default_provider(found)]
    )
    kind = next((n for n, label in labels.items() if picked in (n, label)), picked.split()[0] if picked else "")
    if kind not in PROVIDERS:
        raise ValueError(f"onboard.provider must be one of: {', '.join(PROVIDERS)} (got {picked!r})")
    tiers: dict[str, str]
    if kind == "claude-cli":
        if shutil.which("claude") is None:
            p.say("  NOTE: the `claude` command is not on PATH yet; install Claude Code before the first turn.")
        entry: dict[str, Any] = {"name": "claude-cli", "kind": "claude-cli"}
        tiers = dict(TIER_MODELS["claude-cli"])
    else:
        entry, model = _api_entry(p, kind, do_probe=do_probe)
        known = TIER_MODELS.get(kind, {})
        tiers = {**known, "main": model} if model and known.get("main") == model else ({"main": model} if model else {})
    model = tiers.get("main", "")
    data = {
        "providers": {"entries": [entry]},
        "tiers": {"models": {entry["name"]: tiers} if tiers else {}},
    }
    from k3code import confio

    before = confio.read_yaml(user_config_path()).get("providers") or []
    path = write_config(data, only="providers")  # keeps the rest of an existing config untouched
    p.say(f"Wrote {path}: provider {entry['name']} ({entry['kind']}), default model {model or '(not set yet)'}.")
    if kept := _keep_other_providers(path, before, entry):
        p.say(f"  kept as fallback (after {entry['name']}): {', '.join(kept)}")
    if not model:
        p.say("  No model set yet: add providers[0].models.default with `k3code config-edit`.")
    offer_project_recipes(p, Path.cwd())
    p.say("Ready. `k3code onboard` (choose full) adds more providers, MCP servers and more any time.")


def _keep_other_providers(path: Path, before: list[Any], entry: dict[str, Any]) -> list[str]:
    """Re-append the chain entries fast setup replaced: adding a key used to drop every fallback provider.

    An old entry with the new entry's name or endpoint is the one being replaced and is not kept."""
    from k3code import confio

    base = str(entry.get("base_url") or "").rstrip("/")
    others = [
        e
        for e in before
        if isinstance(e, dict)
        and e.get("name") != entry["name"]
        and not (base and str(e.get("base_url") or "").rstrip("/") == base)
    ]
    if not others:
        return []
    cfg = confio.read_yaml(path)
    cfg["providers"] = [*(cfg.get("providers") or []), *others]
    confio.validate(cfg)
    confio.write_yaml(path, cfg, backup=False)  # write_config already backed up the file as it was
    return [str(e.get("name")) for e in others]


def _run(p: Prompter, answer: str, *, do_probe: bool) -> None:
    if answer not in MODES:
        raise ValueError(f"onboard.mode must be one of: {', '.join(MODES)} (got {answer!r})")
    record_answer(answer)
    if answer == "full":
        run_setup(p, do_probe=do_probe)
    else:
        run_fast(p, do_probe=do_probe)


def run_onboarding(p: Prompter, *, do_probe: bool = True) -> None:
    """``k3code onboard``: ask fast or full and run it. Works at any time, with or without a config."""
    _run(p, p.select("onboard.mode", QUESTION, MODES, "fast"), do_probe=do_probe)


def first_run(p: Prompter, *, do_probe: bool = True) -> None:
    """Plain interactive launch: ask fast or full once while no config exists; never repeat the question.

    A Ctrl+C at the question records it as skipped and re-raises, so the caller can exit.
    """
    if user_config_path().is_file():
        return
    if question_answered():
        p.say(NO_CONFIG_HINT)
        return
    try:
        answer = p.select("onboard.mode", QUESTION, MODES, "fast")
    except (KeyboardInterrupt, EOFError):
        record_answer("skipped")
        raise
    _run(p, answer, do_probe=do_probe)
