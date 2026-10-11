"""Config file I/O shared by /config, /import, /output-style and friends.

Reads/writes YAML, validates a merged dict against the pydantic ``Settings``
(plus the reliability and permissions sub-schemas), and keeps timestamped
backups ``<name>.bak-<ts>`` next to the file before every write.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from k3code import trust
from k3code.config import ConfigError, Settings, _parse_mapping

__all__ = ["ConfigError"]  # one class for "this config is invalid": config.py raises it too

_BAK_RE = re.compile(r"\.bak-\d{8}T\d{12}(?:-\d+)?$")
#: Backups of one config file kept next to it; older ones are deleted after each write.
KEEP_BACKUPS = 10


def read_yaml(path: Path) -> dict[str, Any]:
    """The mapping in ``path`` ({} when absent); unreadable, malformed or non-mapping content is a ConfigError."""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: cannot be read ({e.__class__.__name__})") from e
    return _parse_mapping(text, path)


def backup_path(path: Path) -> Path:
    now = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now)) + f"{int(now * 1e6) % 1_000_000:06d}"
    cand = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while cand.exists():
        cand = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    return cand


def backups(path: Path) -> list[Path]:
    """Backups of ``path``, oldest first."""
    if not path.parent.is_dir():
        return []
    found = [p for p in path.parent.iterdir() if p.name.startswith(path.name + ".bak-") and _BAK_RE.search(p.name)]
    return sorted(found, key=lambda p: p.name)


def latest_backup(path: Path) -> Path | None:
    found = backups(path)
    return found[-1] if found else None


def replace_bytes(path: Path, data: bytes) -> None:
    """Atomically put ``data`` in ``path`` (no backup, no trust bookkeeping): a temp file next to it, then
    ``os.replace``, so a reader sees the old file or the new one, never half of it. A symlinked config stays a symlink
    (the target is replaced); an existing file keeps its mode, a new one gets the umask default."""
    target = path.resolve()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    mode = stat.S_IMODE(target.stat().st_mode) if target.is_file() else None
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if mode is not None:
                os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _prune_backups(path: Path, keep: int = KEEP_BACKUPS) -> None:
    found = backups(path)
    for old in found[: max(0, len(found) - keep)]:
        with contextlib.suppress(OSError):
            old.unlink()


def write_yaml(path: Path, data: dict[str, Any], *, backup: bool = True, keep_trust: bool = True) -> Path | None:
    """Write ``data`` as YAML; the previous file is copied to ``.bak-<ts>`` first. Returns the backup.

    The write is atomic and keeps the file's mode; the backup keeps it too (a 0600 config has 0600 backups) and only
    the newest ``KEEP_BACKUPS`` are kept.

    ``keep_trust=False`` for content someone else wrote (an imported bundle): a project config written that way is
    left untrusted until the user approves it with `k3code trust`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    bak: Path | None = None
    if backup and path.is_file():
        bak = backup_path(path)
        shutil.copy2(path, bak)
    raw = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")
    if keep_trust:
        with trust.keeping_trust(path):
            replace_bytes(path, raw)
    else:
        replace_bytes(path, raw)
    if bak is not None:
        _prune_backups(path)
    return bak


def restore_backup(path: Path, bak: Path) -> None:
    """Make ``bak`` the config again, byte for byte (comments included); the file as it is now is backed up first, so
    a rollback can itself be rolled back. ``bak`` is consumed (the next rollback goes one step further back)."""
    read_yaml(bak)  # a backup that does not parse is not put in place
    if path.is_file():
        shutil.copy2(path, backup_path(path))
    with trust.keeping_trust(path):
        replace_bytes(path, bak.read_bytes())
    bak.unlink()
    _prune_backups(path)


def validate(data: dict[str, Any]) -> None:
    """Raise :class:`ConfigError` unless ``data`` is a valid config mapping."""
    try:
        settings = Settings(**data)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise ConfigError(f"{loc}: {first['msg']}") from e
    _validate_values(settings)
    if data.get("reliability"):
        _validate_reliability(data["reliability"])
    if data.get("permissions"):
        _validate_permissions(data["permissions"])


def _number(
    label: str, value: Any, *, lo: float | None = None, above: float | None = None, hi: float | None = None
) -> None:
    """``value`` is a real number (not a bool, NaN or inf), at least ``lo``, above ``above``, at most ``hi``."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value or abs(value) == float("inf"):
        raise ConfigError(f"{label}: must be a number, got {value!r}")
    if lo is not None and value < lo:
        raise ConfigError(f"{label}: must be {lo:g} or more, got {value!r}")
    if above is not None and value <= above:
        raise ConfigError(f"{label}: must be above {above:g}, got {value!r}")
    if hi is not None and value > hi:
        raise ConfigError(f"{label}: must be at most {hi:g}, got {value!r}")


def _whole(label: str, value: Any, *, lo: int) -> None:
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    if ok and isinstance(value, float):
        ok = value.is_integer()
    if not ok:
        raise ConfigError(f"{label}: must be a whole number, got {value!r}")
    _number(label, value, lo=lo)


def _validate_values(s: Settings) -> None:
    """What the types alone let through: a mode nobody can create a session with, a range that breaks a turn."""
    from k3code.permissions import InvalidPermissionMode, permission_mode_from_config

    for key in ("permission_mode", "headless_permission"):
        value = getattr(s, key)
        if value is not None:
            try:
                permission_mode_from_config(key, value)
            except InvalidPermissionMode as e:
                raise ConfigError(f"{key}: {value!r} is not one of ask, auto-edit, plan, auto, yolo") from e
    _whole("max_tokens", s.max_tokens, lo=1)
    _whole("max_turns", s.max_turns, lo=0)
    if s.temperature is not None:
        _number("temperature", s.temperature, lo=0, hi=2)
    _whole("automation.max_concurrent", s.automation.max_concurrent, lo=1)
    for key, value in s.router.items():
        if key in ("max_inline_wait", "quota_cooldown"):
            _number(f"router.{key}", value, lo=0)
    enabled = s.learning.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"learning.enabled: must be true/false, got {enabled!r}")
    for key in ("usage_days", "journal_days", "debug_bundles", "decisions_days"):
        if key in s.retention:
            _number(f"retention.{key}", s.retention[key], above=0)


def _validate_reliability(rel: Any) -> None:
    from k3code.reliability import ReliabilityFlags, ReliabilitySettings

    if not isinstance(rel, dict):
        raise ConfigError("reliability: must be a mapping")
    known = set(ReliabilitySettings.__dataclass_fields__)
    for key in rel:
        if key not in known:
            raise ConfigError(f"reliability.{key}: unknown key (known: {', '.join(sorted(known))})")
    if "enabled" in rel and not isinstance(rel["enabled"], bool):
        raise ConfigError(f"reliability.enabled: must be true/false, got {rel['enabled']!r}")
    for key in ("max_wait", "max_park_seconds", "session_usd", "session_eur", "day_usd", "day_eur"):
        if rel.get(key) is not None:
            _number(f"reliability.{key}", rel[key], above=0)
    for key in ("session_tokens", "day_tokens"):
        if rel.get(key) is not None:
            _whole(f"reliability.{key}", rel[key], lo=1)
    flags = rel.get("flags")
    if flags is not None:
        fknown = set(asdict(ReliabilityFlags()))
        if not isinstance(flags, dict):
            raise ConfigError("reliability.flags: must be a mapping")
        for key, val in flags.items():
            if key not in fknown:
                raise ConfigError(f"reliability.flags.{key}: unknown flag (known: {', '.join(sorted(fknown))})")
            if not isinstance(val, bool):
                raise ConfigError(f"reliability.flags.{key}: must be true/false")


def _validate_permissions(perms: Any) -> None:
    from k3code.permissions import from_config

    if not isinstance(perms, dict):
        raise ConfigError("permissions: must be a mapping")
    body = {k: v for k, v in perms.items() if k != "hardline"}
    for tool, value in body.items():
        entries = {"*": value} if isinstance(value, str) else value
        if not isinstance(entries, dict):
            raise ConfigError(f"permissions.{tool}: must be an action or a pattern→action mapping")
        for pat, action in entries.items():
            if action not in ("allow", "ask", "deny"):
                raise ConfigError(f"permissions.{tool}.{pat}: action must be allow|ask|deny, got {action!r}")
    from_config(body)
    if "hardline" in perms and not isinstance(perms["hardline"], list):
        raise ConfigError("permissions.hardline: must be a list of regexes")


def parse_value(raw: str) -> Any:
    """CLI value → YAML scalar/list/mapping (``true``, ``3``, ``[a, b]`` …)."""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def get_path(data: Any, key: str) -> Any:
    for part in key.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise KeyError(key)
    return data


def set_path(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
