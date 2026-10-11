"""``/update-config <natural language>``: cheap tier → JSON patch → validate → diff → confirm → apply (with backup)."""

from __future__ import annotations

import copy
import difflib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from k3code import confio
from k3code.config import Settings
from k3code.providers.types import Message
from k3code.routing.tiers import TaskKind

SECRET_KEY = re.compile(r"(api[_-]?key|secret|token|passw|credential|authorization|headers?$|^env$|api_key_env)", re.I)
SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|Bearer\s+\S+)")
RELAXED_MODES = {"yolo"}


class PatchRejected(ValueError):
    pass


SYSTEM = (
    "You translate a request into a JSON merge patch for a coding agent's config. Reply with ONE JSON object only "
    "(the patch). Use only keys from the schema. Setting a key to null removes it. Never touch api keys, tokens, "
    "headers, env, providers or hardline rules. If the request cannot be expressed, reply {}.\nSchema (top-level "
    "keys and defaults):\n"
)


def schema_hint() -> str:
    d = Settings().model_dump()
    d.pop("providers", None)
    return json.dumps(d, default=str)[:3500]


def _walk(patch: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(patch, dict):
        for k, v in patch.items():
            yield (*path, str(k)), v
            yield from _walk(v, (*path, str(k)))
    elif isinstance(patch, list):
        for v in patch:
            yield from _walk(v, path)


def check_patch(patch: dict[str, Any], current: dict[str, Any]) -> None:
    """Raise :class:`PatchRejected` for secrets, unknown keys and hardline-relaxing changes."""
    if not isinstance(patch, dict):
        raise PatchRejected("the patch must be a JSON object")
    known = set(Settings.model_fields)
    for top in patch:
        if top not in known:
            raise PatchRejected(f"unknown config key: {top}")
        if top == "providers":
            raise PatchRejected("providers (endpoints and keys) cannot be changed through /update-config")
    for path, value in _walk(patch):
        if SECRET_KEY.search(path[-1]):
            raise PatchRejected(f"{'.'.join(path)}: secrets and credentials cannot be set here (use env vars)")
        if isinstance(value, str) and SECRET_VALUE.search(value):
            raise PatchRejected(f"{'.'.join(path)}: value looks like a secret")
    for key in ("permission_mode", "headless_permission"):
        if str(patch.get(key, "")).lower() in RELAXED_MODES:
            raise PatchRejected(f"{key}={patch[key]} would relax the permission model; set it with /yolo yourself")
    perms = patch.get("permissions")
    if isinstance(perms, dict):
        if "hardline" in perms:
            old = set((current.get("permissions") or {}).get("hardline") or [])
            new = perms["hardline"]
            if not isinstance(new, list) or not old <= set(map(str, new)):
                raise PatchRejected("permissions.hardline may only be extended, never reduced")
        bash = perms.get("bash")
        if bash == "allow" or (isinstance(bash, dict) and bash.get("*") == "allow"):
            raise PatchRejected("allowing every bash command would relax the safety rules")
    auto = patch.get("autonomy")
    if isinstance(auto, dict) and auto.get("gate_modes") == []:
        raise PatchRejected("autonomy.gate_modes may not be emptied (disables the scope gate)")


def merge_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_patch(out[k], v)
        else:
            out[k] = v
    return out


def diff_text(old: dict[str, Any], new: dict[str, Any]) -> str:
    a = yaml.safe_dump(old, sort_keys=True).splitlines()
    b = yaml.safe_dump(new, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "config (current)", "config (proposed)", lineterm="", n=1))


def parse_patch(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise PatchRejected("the model did not return a JSON patch")
    try:
        data = json.loads(m.group(0))
    except ValueError as e:
        raise PatchRejected("the model returned invalid JSON") from e
    if not isinstance(data, dict):
        raise PatchRejected("the patch must be a JSON object")
    return data


async def translate(caller: Any, request: str, *, session_id: str = "") -> dict[str, Any]:
    res = await caller.complete(
        TaskKind.CLASSIFICATION,
        [Message(role="system", content=SYSTEM + schema_hint()), Message(role="user", content=request[:1500])],
        session_id=session_id,
        max_tokens=600,
        timeout=30,
    )
    return parse_patch(res.text)


def prepare(patch: dict[str, Any], path: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Validate ``patch`` against the user config at ``path``; returns (current, merged, diff)."""
    current = confio.read_yaml(path)
    check_patch(patch, current)
    merged = merge_patch(current, patch)
    try:
        confio.validate(merged)
    except confio.ConfigError as e:
        raise PatchRejected(f"the patch makes the config invalid: {e}") from e
    # a plain value the model returned as text ("5") is written as the number Settings makes of it, so the file, the
    # diff the user confirms and the live config agree
    settings = Settings(**merged)
    for key, value in patch.items():
        if value is not None and not isinstance(value, dict | list) and key in Settings.model_fields:
            merged[key] = getattr(settings, key)
    return current, merged, diff_text(current, merged)


async def run(ctx: Any, session_id: str | None, request: str, path: Path) -> str:
    """The full flow: translate, validate, show the diff, confirm through ``clarify``, apply with a backup."""
    if not request:
        return "Usage: /update-config <what you want changed, in plain words>"
    try:
        patch = await translate(ctx.model_caller, request, session_id=session_id or "")
        if not patch:
            return "I could not turn that into a config change."
        current, merged, diff = prepare(patch, path)
    except PatchRejected as e:
        return f"Rejected: {e}"
    if not diff:
        return "Nothing to change: the config already has those values."
    answer = await ctx.clarify(f"Apply this config change?\n{diff}", ["apply", "cancel"], session_id)
    if str(answer.get("choice") or answer.get("answer") or "").lower() not in ("apply", "yes", "y"):
        return "Cancelled; config unchanged.\n" + diff
    bak = confio.write_yaml(path, merged)
    if getattr(ctx, "learning", None) is not None:
        live = ctx.sessions.get(session_id) if session_id else None
        ctx.learning.record("config", live, subject="update-config", choice="apply", detail={"keys": sorted(patch)})
    done = f"Config updated ({path}); backup: {bak or 'none (new file)'}\n{diff}"
    try:
        _apply_live(ctx, merged, patch)
    except Exception as e:  # noqa: BLE001 - the file is written; say that the running config did not follow
        return f"{done}\nBut the running config could not be updated ({e}); it applies at the next start."
    return done


def _apply_live(ctx: Any, merged: dict[str, Any], patch: dict[str, Any]) -> None:
    """Make the running config follow the file the way /config set does: through ``apply_file_config`` (the files are
    re-read and every value goes through Settings, so a section like `goal` or `display` and a coerced number are
    right). A context without it (no server) takes the patched keys from the validated settings."""
    apply = getattr(ctx, "apply_file_config", None)
    if apply is not None:
        apply()
        return
    settings = Settings(**merged)
    for key in patch:
        setattr(ctx.config, key, getattr(settings, key))
