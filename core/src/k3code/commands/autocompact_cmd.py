"""/autocompact: when a session folds its older messages into a summary on its own."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from k3code import confio
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, split_args
from k3code.context_budget import (
    COMPACT_AT_RATIO,
    MAX_COMPACT_RATIO,
    MIN_COMPACT_RATIO,
    MIN_COMPACT_TOKENS,
    AutoCompact,
    autocompact_policy,
    compact_threshold,
    context_window,
)
from k3code.paths import user_config_path

USAGE = (
    "Usage: /autocompact [auto | off | on | <tokens>[k|m] | <percent>% | reset] [--session]\n"
    "  auto         compact at 70% of the model's context window (the default)\n"
    "  120k, 60%    compact once the request reaches that many tokens, or that share of the window\n"
    "  off          never compact on its own: run /compact yourself (a request the model rejects as too long\n"
    "               then fails with a prompt to do so)\n"
    "  --session    this session only, not saved; /autocompact reset --session drops it again"
)

_LIMIT = re.compile(r"^(\d+(?:\.\d+)?)\s*(k|m|%)?$", re.IGNORECASE)


class AutoCompactError(ValueError):
    """The argument is no setting /autocompact knows; the message says what is wrong."""


def parse_changes(word: str) -> dict[str, Any] | None:
    """What ``word`` changes in the ``context`` section: key -> new value, None = remove the key (the default applies).
    None for ``reset``. Raises :class:`AutoCompactError` on anything else."""
    text = word.strip().lower().replace(",", "").replace("_", "")
    if text in ("auto", "default"):
        return {"autocompact": None, "compact_at_tokens": None, "compact_at_ratio": None}
    if text in ("on", "enable", "enabled", "true"):
        return {"autocompact": None}  # on again, at the limit it had
    if text in ("off", "disable", "disabled", "false", "never"):
        return {"autocompact": False}
    if text == "reset":
        return None
    match = _LIMIT.match(text)
    if match is None:
        raise AutoCompactError(f"Unknown setting: {word}")
    number, unit = float(match.group(1)), (match.group(2) or "").lower()
    if unit == "%":
        ratio = number / 100
        if not MIN_COMPACT_RATIO <= ratio <= MAX_COMPACT_RATIO:
            raise AutoCompactError(
                f"{word}: a share of the window between {MIN_COMPACT_RATIO:.0%} and {MAX_COMPACT_RATIO:.0%}"
            )
        return {"autocompact": None, "compact_at_tokens": None, "compact_at_ratio": round(ratio, 4)}
    tokens = int(number * {"": 1, "k": 1_000, "m": 1_000_000}[unit])
    if tokens < MIN_COMPACT_TOKENS:
        hint = f" (for a share of the window, write {number:g}%)" if not unit and 0 < number <= 100 else ""
        raise AutoCompactError(f"{word}: a limit is at least {MIN_COMPACT_TOKENS:,} tokens{hint}")
    return {"autocompact": None, "compact_at_tokens": tokens, "compact_at_ratio": None}


def apply_changes(section: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """``section`` (a ``context`` mapping) with ``changes`` applied."""
    out = dict(section)
    for key, value in changes.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def write_user_config(changes: dict[str, Any]) -> tuple[Any, Any]:
    """Save ``changes`` into the user's config.yaml (validated first, the old file kept as a backup)."""
    path = user_config_path()
    data = confio.read_yaml(path)
    section = data.get("context")
    section = apply_changes(section if isinstance(section, dict) else {}, changes)
    if section:
        data["context"] = section
    else:
        data.pop("context", None)
    confio.validate(data)
    return path, confio.write_yaml(path, data)


def _context_of(path: Any) -> dict[str, Any]:
    """The ``context`` section of the config file at ``path`` (empty when it has none)."""
    section = confio.read_yaml(path).get("context")
    return section if isinstance(section, dict) else {}


def describe(policy: AutoCompact, config: Any, model: str, used: int | None = None) -> str:
    """One status paragraph: whether and when it compacts, and how far the session is from that."""
    if not policy.enabled:
        return "Auto-compact is off: the context grows until you run /compact."
    window = context_window(config, model)
    limit = compact_threshold(config, model, policy)
    if policy.tokens:
        how = f"at {limit:,} tokens"
    elif policy.ratio:
        how = f"at {policy.ratio:.0%} of the window ({limit:,} of {window:,} tokens for {model})"
    else:
        how = f"auto: at {COMPACT_AT_RATIO:.0%} of the window ({limit:,} of {window:,} tokens for {model})"
    text = f"Auto-compact is on, {how}."
    if limit >= window:
        text += f" That is past the {window:,}-token window of {model}: the provider rejects the request first."
    if used is not None:
        text += f"\nThis session's next request is ~{used:,} tokens: " + (
            f"~{limit - used:,} left before it compacts." if used < limit else "it compacts before the next turn."
        )
    return text


class AutoCompactCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="autocompact",
            help="Show or set when the conversation is compacted on its own: /autocompact [auto|off|120k|60%]",
            aliases=["auto-compact"],
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        session_only = pop_flag(args, "--session")
        if len(args) > 1 or any(a.startswith("--") for a in args):
            return reply(USAGE)
        live = ctx.sessions.get(session_id) if session_id else None
        if session_only and live is None:
            return reply("No active session: --session needs one.")
        if not args:
            return reply(self._status(ctx, live), autocompact=self._payload(ctx, live))
        try:
            changes = parse_changes(args[0])
        except AutoCompactError as e:
            return reply(f"{e}\n{USAGE}")
        if session_only:
            if changes is None:
                live.stored.meta.pop("autocompact", None)
                lead = "Auto-compact is back to the config's setting for this session."
            else:
                now = autocompact_policy(ctx.config, live.stored.meta.get("autocompact"))
                section = {"autocompact": now.enabled, "compact_at_tokens": now.tokens, "compact_at_ratio": now.ratio}
                new = autocompact_policy(SimpleNamespace(context=apply_changes(section, changes)))
                live.stored.meta["autocompact"] = {"enabled": new.enabled, "tokens": new.tokens, "ratio": new.ratio}
                lead = "For this session only:"
            ctx.store.save(live.stored)
        else:
            if changes is None:
                return reply("reset only applies to a session's own setting: /autocompact reset --session")
            try:
                path, backup = write_user_config(changes)
            except confio.ConfigError as e:
                return reply(f"Invalid config: {e}")
            ctx.apply_file_config(live.perms.cwd if live is not None else None)
            lead = f"Saved in {path}" + (f" (backup: {backup.name})." if backup else ".")
            wanted = autocompact_policy(SimpleNamespace(context=_context_of(path)))
            if autocompact_policy(ctx.config) != wanted:  # a project config's `context:` section replaces this one
                lead += (
                    " A `context:` section in this project's .k3code/config.yaml replaces it here: "
                    "edit that, or use --session."
                )
        return reply(f"{lead}\n{self._status(ctx, live)}", autocompact=self._payload(ctx, live))

    @staticmethod
    def _model(ctx: Any, live: Any) -> str:
        return ctx._active_model(live) if live is not None else ctx.config.default_model

    def _status(self, ctx: Any, live: Any) -> str:
        policy = autocompact_policy(ctx.config, live.stored.meta.get("autocompact") if live is not None else None)
        used = ctx._request_tokens(live) if live is not None else None
        text = describe(policy, ctx.config, self._model(ctx, live), used)
        if live is not None and live.stored.meta.get("autocompact"):
            text += "\nSet for this session only (/autocompact reset --session undoes that)."
        return text

    def _payload(self, ctx: Any, live: Any) -> dict[str, Any]:
        policy = autocompact_policy(ctx.config, live.stored.meta.get("autocompact") if live is not None else None)
        model = self._model(ctx, live)
        return {
            "enabled": policy.enabled,
            "mode": policy.mode,
            "threshold": compact_threshold(ctx.config, model, policy),
            "window": context_window(ctx.config, model),
            "session_only": bool(live is not None and live.stored.meta.get("autocompact")),
        }
