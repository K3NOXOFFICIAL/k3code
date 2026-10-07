"""``/model chain``: show the provider/model fallback chain, and edit it in the user config.

Edits go validate → backup → write: the new provider list is validated by building ``Settings`` from it,
the existing ``config.yaml`` is copied to ``config.yaml.bak.<ts>``, then the new file is written.
(YAML comments in the user config are not preserved by the rewrite; the backup keeps them.)
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import yaml

from k3code.config import ProviderEntry, Settings
from k3code.daemon import k3_home


class ChainEditError(Exception):
    """A requested chain edit is invalid (message is shown to the user)."""


def config_path(home: Path | None = None) -> Path:
    return (home or k3_home()) / "config.yaml"


def _models_list(entry: dict[str, Any], key: str = "default") -> list[str]:
    spec = (entry.get("models") or {}).get(key)
    if spec is None:
        return []
    return [spec] if isinstance(spec, str) else list(spec)


def _set_models(entry: dict[str, Any], models: list[str], key: str = "default") -> None:
    entry.setdefault("models", {})[key] = models[0] if len(models) == 1 else models


def _find(providers: list[dict[str, Any]], name: str) -> int:
    for i, p in enumerate(providers):
        if p.get("name") == name:
            return i
    raise ChainEditError(f"unknown provider: {name} (have: {', '.join(p.get('name', '?') for p in providers)})")


def apply_edit(providers: list[dict[str, Any]], op: str, args: list[str]) -> list[dict[str, Any]]:
    """Return an edited copy of the raw provider list for ``add|remove|move``."""
    out = [dict(p, models=dict(p.get("models") or {})) for p in providers]
    if op == "add":
        if len(args) < 2:
            raise ChainEditError("usage: /model chain add <provider> <model> [kind=… base_url=… key_env=…]")
        name, model, *rest = args
        extra = dict(a.split("=", 1) for a in rest if "=" in a)
        try:
            entry = out[_find(out, name)]
        except ChainEditError:
            if not {"kind", "base_url", "key_env"} <= set(extra):
                raise ChainEditError(
                    f"{name} is not in the chain; to add it give kind=openai|anthropic base_url=… key_env=ENV_VAR"
                ) from None
            entry = {
                "name": name,
                "kind": extra["kind"],
                "base_url": extra["base_url"],
                "api_key_env": extra["key_env"],
                "models": {},
            }
            out.append(entry)
        models = _models_list(entry)
        if model not in models:
            models.append(model)
        _set_models(entry, models)
    elif op == "remove":
        if not args:
            raise ChainEditError("usage: /model chain remove <provider> [model]")
        i = _find(out, args[0])
        if len(args) > 1:
            models = [m for m in _models_list(out[i]) if m != args[1]]
            if len(models) == len(_models_list(out[i])):
                raise ChainEditError(f"{args[0]} has no model {args[1]}")
            if models:
                _set_models(out[i], models)
            else:
                del out[i]
        else:
            del out[i]
    elif op == "move":
        if len(args) != 2 or not args[1].isdigit():
            raise ChainEditError("usage: /model chain move <provider> <position>  (1 = first)")
        entry = out.pop(_find(out, args[0]))
        out.insert(max(0, min(int(args[1]) - 1, len(out))), entry)
    else:
        raise ChainEditError(f"unknown chain op: {op} (add|remove|move)")
    return out


def edit_config(op: str, args: list[str], home: Path | None = None) -> str:
    """Validate, back up, then write; returns the backup path ('' when there was no file)."""
    path = config_path(home)
    data: dict[str, Any] = (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}
    providers = data.get("providers")
    if not providers:
        raise ChainEditError(f"no providers in {path}; the chain is defined elsewhere (project config?)")
    new = apply_edit(providers, op, args)
    if not new:
        raise ChainEditError("that would leave the chain empty")
    # 1. validate (raises pydantic ValidationError → shown as the failure)
    try:
        Settings(providers=[ProviderEntry(**{**p, "api_key": ""}) for p in new])
    except Exception as e:  # noqa: BLE001
        raise ChainEditError(f"invalid chain: {e}") from e
    # 2. backup  3. write
    backup = ""
    if path.is_file():
        backup = f"{path}.bak.{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
    data["providers"] = new
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return backup


def chain_rows(ctx: Any, session_id: str | None) -> list[dict[str, Any]]:
    """The effective chain in walk order, each row with live cooldown and network health."""
    config = ctx.config
    key = config.default_model
    live = ctx.sessions.get(session_id) if session_id and hasattr(ctx, "sessions") else None
    netwatch = getattr(getattr(live, "reliability", None), "netwatch", None)
    cooldowns = getattr(ctx, "cooldowns", None)
    rows: list[dict[str, Any]] = []
    for p in config.providers:
        spec = p.models.get(key) or p.models.get("default") or next(iter(p.models.values()), "")
        for model in [spec] if isinstance(spec, str) else list(spec):
            remaining = 0.0
            if cooldowns is not None:
                remaining = cooldowns.remaining_seconds(provider=p.name, model=model, base_url=p.base_url)
            state = netwatch.provider_state(p.name) if netwatch is not None else None
            # A connectivity verdict (provider_down / offline / captive) says *why* the entry is cooling down.
            if state is not None and state.value in ("provider_down", "offline", "captive"):
                health = state.value
            elif remaining > 0:
                health = "cooldown"
            else:
                health = state.value if state is not None else "unknown"
            rows.append(
                {
                    "position": len(rows) + 1,
                    "provider": p.name,
                    "model": model,
                    "base_url": p.base_url,
                    "cooldown_s": round(remaining, 1),
                    "health": health,
                }
            )
    return rows


def format_chain(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No providers configured."
    lines = ["Fallback chain (tried in order):"]
    for r in rows:
        extra = f" — cooling down {r['cooldown_s']:.0f}s" if r["cooldown_s"] > 0 else ""
        lines.append(f"{r['position']}. {r['provider']}/{r['model']} [{r['health']}]{extra}  {r['base_url']}")
    return "\n".join(lines)
