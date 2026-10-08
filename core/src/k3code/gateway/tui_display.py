"""TUI display settings over ``config.set`` / ``config.get``.

The TUI's /theme, /indicator, /statusbar, /battery, /mouse, /density, /busy, /details and /pet send short keys
(``indicator``, ``statusbar``, ...). Each maps to a ``display.<field>`` the TUI reads back from ``config.full``.
A set updates the live config and the user config file, so the choice survives a restart.
"""

from __future__ import annotations

import logging
from typing import Any

from k3code import confio
from k3code.paths import user_config_path

logger = logging.getLogger(__name__)

# TUI key -> (display field, kind). kind "bool" accepts true/false/on/off; "str" any short string.
KEYS: dict[str, tuple[str, str]] = {
    "theme": ("tui_theme", "str"),
    "indicator": ("tui_status_indicator", "str"),
    "statusbar": ("tui_statusbar", "str"),
    "battery": ("battery", "bool"),
    "mouse": ("mouse_tracking", "bool"),
    "density": ("tui_compact", "bool"),
    "busy": ("busy_input_mode", "str"),
    "details_mode": ("details_mode", "str"),
    "pet": ("pet", "str"),
}

_TRUE = ("1", "true", "on", "yes")
_FALSE = ("0", "false", "off", "no")


class DisplayValueError(ValueError):
    pass


def _field(key: str) -> tuple[str, str] | None:
    """``details_mode.<section>`` stores under ``display.sections.<section>``."""
    if key in KEYS:
        return KEYS[key]
    if key.startswith("details_mode.") and key.count(".") == 1:
        return f"sections.{key.split('.', 1)[1]}", "str"
    return None


def handles(key: str) -> bool:
    return _field(key) is not None


def _coerce(raw: Any, kind: str) -> Any:
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise DisplayValueError(f"expected on or off, got {raw!r}")
    if raw is None:
        return ""
    if not isinstance(raw, str | int | float) or len(str(raw)) > 64:
        raise DisplayValueError(f"expected a short string, got {raw!r}")
    return str(raw).strip()


def _wire(value: Any) -> str:
    """The TUI treats an empty ``value`` as failure; booleans go back as on/off."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return "" if value is None else str(value)


def get(display: Any, key: str) -> dict[str, Any]:
    field, _ = _field(key) or ("", "")
    value: Any = display.model_dump()
    for part in field.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return {"value": value}


def set_(display: Any, key: str, raw: Any) -> dict[str, Any]:
    field, kind = _field(key) or ("", "")
    value = _coerce(raw, kind)
    if "." in field:  # sections.<name>
        top, sub = field.split(".", 1)
        current = dict(getattr(display, top, None) or {})
        if value:
            current[sub] = value
        else:
            current.pop(sub, None)
        setattr(display, top, current)
    else:
        setattr(display, field, value)
    _persist(f"display.{field}", value)
    return {"ok": True, "key": key, "value": _wire(value) or "default"}


def _persist(path: str, value: Any) -> None:
    """Write one display key into the user config. A failure keeps the live change and is logged."""
    cfg = user_config_path()
    try:
        data = confio.read_yaml(cfg)
        confio.set_path(data, path, value)
        confio.write_yaml(cfg, data, backup=False)
    except Exception as e:  # noqa: BLE001 - a display preference must never fail the RPC
        logger.warning("could not save %s to %s: %s", path, cfg, e)
