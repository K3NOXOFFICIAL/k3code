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
from k3code.setup.steps import Ctx, _entry_from, write_config
from k3code.setup.wizard import run_setup

QUESTION = "Fast setup (API endpoint + key) or full setup?"
MODES = ["fast", "full"]
CLAUDE_CLI_MODEL = "sonnet"  # a Claude Code alias: resolves to the current Sonnet
DEFAULT_ENDPOINT = probe.PRESETS["omniroute"]["base_url"]
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


def _api_entry(p: Prompter, *, do_probe: bool) -> tuple[dict[str, Any], str]:
    """Ask for the endpoint and key; return the provider entry and its default model id ("" if unknown)."""
    base = p.text("onboard.endpoint", "API endpoint (base URL)", DEFAULT_ENDPOINT) or DEFAULT_ENDPOINT
    base = base.strip().rstrip("/")
    preset = next((n for n, s in probe.PRESETS.items() if s["base_url"].rstrip("/") == base), "custom")
    known = probe.PRESETS.get(preset, {})
    spec: dict[str, Any] = {
        "preset": preset,
        "name": preset if preset != "custom" else "endpoint",
        "base_url": base,
        "kind": known.get("kind", "openai"),
        "api_key_env": known.get("api_key_env", "K3CODE_API_KEY"),
    }
    key = p.text(
        "onboard.key",
        f"API key (hidden; stored 0600 in {env_file_path()}; blank = set the variable yourself)",
        "",
        secret=True,
    ).strip()
    if key:
        spec["api_key"] = key
    entry = _entry_from(Ctx(p=p, data={}), 0, "primary", spec)
    if entry is None:  # only the interactive branch of _entry_from returns None
        raise ValueError("no provider entry was built")
    env_name = spec["api_key_env"]
    if not key and not os.environ.get(env_name) and not read_env_file().get(env_name):
        p.say(f"  NOTE: {env_name} is not set yet. Export it, or add it to {env_file_path()}.")

    model = ""
    if do_probe:
        ok, _ms, ids, detail = probe.list_models(entry, key or os.environ.get(env_name, ""))
        if ok and ids:
            model = ids[0]  # the first id the endpoint lists; change it with `k3code config-edit`
            p.say(f"  model: {model} (first of {len(ids)} the endpoint lists)")
        else:
            p.say(f"  could not list models ({detail})")
    if not model:
        model = p.text("onboard.model", "Model id for everyday work (blank = set it later)", "").strip()
    return entry, model


def run_fast(p: Prompter, *, do_probe: bool = True) -> None:
    """Fast setup: one provider (an endpoint with its key, or claude-cli), defaults for everything else."""
    kind = p.select(
        "onboard.provider",
        "Provider: an API endpoint with a key, or claude-cli (your local Claude Code login)?",
        ["api", "claude-cli"],
        "api",
    )
    if kind == "claude-cli":
        if shutil.which("claude") is None:
            p.say("  NOTE: the `claude` command is not on PATH yet; install Claude Code before the first turn.")
        entry: dict[str, Any] = {"name": "claude-cli", "kind": "claude-cli"}
        model = CLAUDE_CLI_MODEL
    else:
        entry, model = _api_entry(p, do_probe=do_probe)
    data = {
        "providers": {"entries": [entry]},
        "tiers": {"models": {entry["name"]: {"main": model}} if model else {}},
    }
    path = write_config(data, only="providers")  # keeps the rest of an existing config untouched
    p.say(f"Wrote {path}: provider {entry['name']} ({entry['kind']}), default model {model or '(not set yet)'}.")
    if not model:
        p.say("  No model set yet: add providers[0].models.default with `k3code config-edit`.")
    p.say("Start with `k3code`. `k3code onboard` (choose full) adds the rest any time.")


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
