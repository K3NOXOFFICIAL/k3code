"""/tune: model, reasoning effort and ultracode mode in one command.

One logic behind four entry points: ``/tune`` itself, ``/model <key>`` and ``/effort <level>`` (which delegate here),
the ``tune.get`` / ``tune.set`` RPCs the TUI's tune popup uses, and ``config.set model`` (the old picker's string).
A request is validated as a whole first and applied all-or-nothing; model and effort take effect from the next turn,
the ultracode mode from the next prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from k3code import chain_config
from k3code.commands import CommandDef
from k3code.providers.effort import LEVELS as EFFORT_LEVELS
from k3code.providers.effort import takes_effort
from k3code.routing.tiers import Tier, tier_model_specs
from k3code.wakewords import WAKE_WORDS

#: ``LiveSession.ultra_mode`` values: ``ultracode`` runs the ultracode pipeline on typed prompts.
ULTRA_MODES = ("off", "ultracode")
#: ``session``: this session only; ``default``: also the default model of new sessions (written to the user config).
SCOPES = ("session", "default")
EFFORT_WORDS = (*EFFORT_LEVELS, "default")  # ``default`` = send no effort
#: What the old model picker and the Hermes scale call levels this core has no use for; accepted after ``effort`` only.
LEGACY_EFFORT = {"minimal": "low", "ultra": "max", "none": "default"}
_ON = frozenset({"on", "true", "yes", "1"})
_OFF = frozenset({"off", "false", "no", "0"})

USAGE = "/tune [model <key> | <key>] [effort <level> | <level>] [ultracode [on|off]] [--global]"
_USAGE_NOTE = f"levels: {', '.join(EFFORT_WORDS)}; --global also makes the model the default for new sessions"


class TuneError(ValueError):
    """A request is invalid; the message is shown to the user and nothing was applied."""


@dataclass(frozen=True)
class TuneRequest:
    """What to change. ``None`` leaves a field alone; ``effort`` is a level or ``default`` (send no effort)."""

    model: str | None = None
    effort: str | None = None
    ultra_mode: str | None = None
    scope: str = "session"
    reason: str = ""  # free text after ``/model <key>``, kept with the model_switch record

    @property
    def empty(self) -> bool:
        return self.model is None and self.effort is None and self.ultra_mode is None


@dataclass
class TuneOutcome:
    changed: list[str]  #: fields whose value differs now: model, default_model, effort, ultra_mode
    lines: list[str]  #: the reply, one line per requested field


def known_model_keys(config: Any) -> set[str]:
    return {m for p in config.providers for m in p.models} | {config.default_model}


def unknown_model_message(config: Any, key: str) -> str:
    return f"Unknown model key: {key} (known: {', '.join(sorted(known_model_keys(config)))})"


def normalize_effort(value: str, *, legacy: bool = False) -> str:
    """``value`` as one of :data:`EFFORT_WORDS`; ``legacy`` also takes minimal / ultra / none."""
    v = value.strip().lower()
    if legacy:
        v = LEGACY_EFFORT.get(v, v)
    if v not in EFFORT_WORDS:
        raise TuneError(f"Unknown effort: {value}")
    return v


def _once(current: str | None, value: str, what: str) -> str:
    if current is not None and current != value:
        raise TuneError(f"{what} given twice ({current}, {value})")
    return value


def parse_tune(config: Any, arg: str) -> TuneRequest:
    """The ``/tune`` grammar: tokens in any order, nothing applied here. Raises :class:`TuneError`."""
    known = known_model_keys(config)
    tokens = arg.split()
    model = effort = ultra = scope = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        i += 1
        if low in ("--global", "--session"):
            new = "default" if low == "--global" else "session"
            if scope not in (None, new):
                raise TuneError("--global and --session cannot be combined")
            scope = new
        elif low == "--tui-session":
            continue  # what the old picker added; every /tune is for the session it is typed in
        elif low in ("model", "effort", "--reasoning", "--provider"):
            if i >= len(tokens):
                raise TuneError(f"{tok} needs a value")
            value = tokens[i]
            i += 1
            if low == "model":
                if value not in known:
                    raise TuneError(unknown_model_message(config, value))
                model = _once(model, value, "model")
            elif low != "--provider":  # --provider <slug> is accepted and ignored, like the old picker's string
                effort = _once(effort, normalize_effort(value, legacy=True), "effort")
        elif low == "ultracode":
            word = "on"
            if i < len(tokens) and tokens[i].lower() in _ON | _OFF:
                word = tokens[i].lower()
                i += 1
            ultra = _once(ultra, "ultracode" if word in _ON else "off", "ultracode")
        elif tok in known:  # a model key wins over an effort word
            model = _once(model, tok, "model")
        elif low in EFFORT_WORDS:
            effort = _once(effort, low, "effort")
        else:
            raise TuneError(f"Unknown token: {tok}")
    if scope == "default" and model is None:
        raise TuneError("--global needs a model: it makes the model the default for new sessions")
    return TuneRequest(model=model, effort=effort, ultra_mode=ultra, scope=scope or "session")


def request_from_params(params: dict[str, Any]) -> TuneRequest:
    """The ``tune.set`` params as a request (types checked; values are validated by :func:`apply_tune`)."""
    values: dict[str, str | None] = {}
    for field in ("model", "effort", "ultra_mode", "scope"):
        value = params.get(field)
        if value is not None and not isinstance(value, str):
            raise TuneError(f"{field} must be a string")
        values[field] = value or None  # null and "" both mean: leave it alone
    scope = values["scope"] or "session"
    if scope not in SCOPES:
        raise TuneError(f"unknown scope: {scope} (one of {', '.join(SCOPES)})")
    return TuneRequest(model=values["model"], effort=values["effort"], ultra_mode=values["ultra_mode"], scope=scope)


def current_model(config: Any, live: Any) -> str:
    return (live.stored.model if live is not None else "") or config.default_model


def _chain_for(config: Any, key: str) -> list[tuple[Any, str]]:
    """(provider, model id) pairs a turn on ``key`` sends to, in chain order.

    Exactly the specs the routers are built from (``TierRouters``, ``GatewayServer._active_model``): every provider
    contributes one, and one that does not define ``key`` falls back to its ``default`` model, so the first pair is the
    model the turn goes to first even when only a later provider defines the key.
    """
    specs = tier_model_specs(config, Tier.MAIN, key=key)  # one per provider
    return [
        (p, model)
        for p, spec in zip(config.providers, specs, strict=True)
        for model in ([spec] if isinstance(spec, str) else spec)
        if model
    ]


def _label(config: Any, key: str) -> str:
    """The first model id ``key`` routes to, '' when none."""
    pairs = _chain_for(config, key)
    return pairs[0][1] if pairs else ""


def state_line(config: Any, live: Any) -> str:
    key = current_model(config, live)
    label = _label(config, key)
    model = f"{key} ({label})" if label and label != key else key
    if live is None:
        return f"Model: {model} · Effort and ultracode need an active session"
    effort = live.reasoning_effort or "default"
    return f"Model: {model} · Effort: {effort} · Ultracode: {'on' if live.ultra_mode == 'ultracode' else 'off'}"


def apply_tune(ctx: Any, live: Any, req: TuneRequest) -> TuneOutcome:
    """Validate ``req``, then apply it to ``live`` (None: no session) and the config. Raises :class:`TuneError`
    before anything changed; a default model that cannot be saved applies nothing either."""
    config = ctx.config
    if req.model is not None and req.model not in known_model_keys(config):
        raise TuneError(unknown_model_message(config, req.model))
    if req.effort is not None and req.effort not in EFFORT_WORDS:
        raise TuneError(f"Unknown effort: {req.effort}")
    if req.ultra_mode is not None and req.ultra_mode not in ULTRA_MODES:
        raise TuneError(f"Unknown ultracode mode: {req.ultra_mode} (one of {', '.join(ULTRA_MODES)})")
    if req.scope not in SCOPES:
        raise TuneError(f"unknown scope: {req.scope} (one of {', '.join(SCOPES)})")
    if (req.effort is not None or req.ultra_mode is not None) and live is None:
        raise TuneError("No active session.")

    saved = ""
    wrote = False
    if req.model is not None and req.scope == "default":
        try:
            wrote, backup = chain_config.set_default_model(req.model)
        except chain_config.ChainEditError as e:
            raise TuneError(f"Could not save the default model: {e}") from e
        where = str(chain_config.config_path())
        saved = f" (saved to {where}" + (f", backup: {backup})" if backup else ")")

    changed: list[str] = []
    lines: list[str] = []
    session_changed = False
    if req.model is not None:
        key = req.model
        old = current_model(config, live)
        if hasattr(ctx, "learning") and key != old:
            kind = getattr(live, "current_kind", "") if live is not None else ""
            ctx.learning.record(
                "model_switch",
                live,
                subject=f"{old} -> {key}",
                choice=key,
                detail={"from": old, "to": key, "reason": req.reason.strip(), "task_kind": kind or ""},
            )
        if live is not None and key != old:
            changed.append("model")
        if (live is None or req.scope == "default") and (config.default_model != key or wrote):
            changed.append("default_model")
        if live is None or req.scope == "default":
            config.default_model = key
        if live is not None:
            live.stored.model = key
            session_changed = True
        lines.append(f"Model key set to: {key}")
        if req.scope == "default":
            lines.append(f"Default model for new sessions: {key}{saved}")
    if req.effort is not None:
        # Sent from the next turn on: as output_config.effort to Claude models that take it, and as
        # reasoning_effort to OpenAI reasoning models. Other models ignore it.
        value = None if req.effort == "default" else req.effort
        if live.reasoning_effort != value:
            changed.append("effort")
        live.reasoning_effort = value
        if value is None:
            live.stored.meta.pop("reasoning_effort", None)
        else:
            live.stored.meta["reasoning_effort"] = value
        session_changed = True
        lines.append(f"Reasoning effort set to: {req.effort}")
    if req.ultra_mode is not None:
        if live.ultra_mode != req.ultra_mode:
            changed.append("ultra_mode")
        live.ultra_mode = req.ultra_mode
        if req.ultra_mode == "off":
            live.stored.meta.pop("ultra_mode", None)
        else:
            live.stored.meta["ultra_mode"] = req.ultra_mode
        session_changed = True
        lines.append(f"Ultracode mode set to: {'on' if req.ultra_mode == 'ultracode' else 'off'}")
    if session_changed:
        ctx.store.save(live.stored)
    if live is not None and changed:
        live.emit("session.info", live.live_info())
    return TuneOutcome(changed=changed, lines=lines)


def _takes(takes: list[bool | None]) -> bool | None:
    """Whether the effort level matters for a key: True when any model on its chain takes one, False when none does."""
    if any(t is True for t in takes):
        return True
    return False if takes and all(t is False for t in takes) else None


def _model_rows(config: Any, current: str) -> list[dict[str, Any]]:
    keys = [config.default_model]
    for p in config.providers:
        keys.extend(k for k in p.models if k not in keys)
    if current not in keys:
        keys.append(current)
    rows = []
    for key in keys:
        pairs = _chain_for(config, key)
        takes = [takes_effort(model, p.kind) for p, model in pairs]
        rows.append(
            {
                "key": key,
                "providers": list(dict.fromkeys(p.name for p, _ in pairs)),
                "resolved": list(dict.fromkeys(model for _, model in pairs)),
                "description": next((p.descriptions[key] for p in config.providers if p.descriptions.get(key)), ""),
                "current": key == current,
                "default": key == config.default_model,
                "effort": _takes(takes),
            }
        )
    return rows


def snapshot(ctx: Any, live: Any) -> dict[str, Any]:
    """The ``tune.get`` result: everything the tune popup shows."""
    config = ctx.config
    wake = getattr(config, "wake_words", None) or {}
    return {
        "model": current_model(config, live),
        "default_model": config.default_model,
        "has_session": live is not None,
        "models": _model_rows(config, current_model(config, live)),
        "efforts": list(EFFORT_LEVELS),
        "effort": live.reasoning_effort if live is not None else None,
        "ultra_modes": list(ULTRA_MODES),
        "ultra_mode": live.ultra_mode if live is not None else "off",
        "wake_words": [w for w in WAKE_WORDS if wake.get(w) is not False],
        "wake_words_enabled": wake.get("enabled") is not False,
    }


class TuneCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="tune",
            help="Set model, effort and ultracode together: /tune [model] [effort] [ultracode [on|off]] [--global]",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        live = ctx.sessions.get(session_id) if session_id else None
        try:
            req = parse_tune(ctx.config, arg)
        except TuneError as e:
            return {"type": "message", "message": f"{e}. Usage: {USAGE} ({_USAGE_NOTE})"}
        if req.empty:  # the TUI opens its popup for a bare /tune; any other client gets the state
            return {"type": "message", "message": state_line(ctx.config, live)}
        try:
            outcome = apply_tune(ctx, live, req)
        except TuneError as e:
            return {"type": "message", "message": str(e)}
        return {"type": "message", "message": "\n".join(outcome.lines)}
