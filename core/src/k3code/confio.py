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
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from k3code import trust
from k3code.config import Settings
from k3code.paths import ensure_state_dir

try:
    import fcntl
except ImportError:  # Windows: no flock, so the lock below is a no-op there (the atomic write still holds)
    fcntl = None  # type: ignore[assignment]

_BAK_RE = re.compile(r"\.bak-\d{8}T\d{12}(?:-\d+)?$")
_held = threading.local()


class ConfigError(ValueError):
    """A config value failed validation."""


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` (a ``.<name>.lock`` file beside it) for a read-modify-write.

    Serialises k3code processes and threads that edit the same config file, so two edits do not each read the old
    file and the second one drop the first one's change. Reentrant within a thread (``write_yaml`` takes it again
    under an outer ``with locked(path)``). A best effort: where ``fcntl`` is missing (Windows) it does nothing, and a
    lock file that cannot be opened is skipped rather than failing the edit.
    """
    key = str(path.absolute())
    held: set[str] = _held.__dict__.setdefault("paths", set())
    if key in held or fcntl is None:
        yield
        return
    try:
        ensure_state_dir(path.parent)
        fd = os.open(path.parent / f".{path.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except (OSError, RuntimeError):  # RuntimeError: a $K3CODE_HOME another user owns
        yield
        return
    held.add(key)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        held.discard(key)
        os.close(fd)  # closing the descriptor releases the flock


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``: temp file in the same directory, fsync, ``os.replace``.

    A plain ``write_text`` truncates first, so a kill or a full disk in the middle left an empty or partial file. An
    existing file keeps its mode (and is written under 0600 until then, so a 0600 file is never briefly readable); a
    new file gets 0666 minus the umask. A symlink is written through.
    """
    target = path.resolve() if path.is_symlink() else path
    mode = target.stat().st_mode & 0o7777 if target.exists() else None
    tmp = target.with_name(f".{target.name}.tmp-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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


def write_yaml(path: Path, data: dict[str, Any], *, backup: bool = True, keep_trust: bool = True) -> Path | None:
    """Write ``data`` as YAML, atomically; the previous file is copied to ``.bak-<ts>`` first. Returns the backup.

    A write that would not change the file's bytes makes no backup (and returns None): rewriting the same config
    must not pile up identical ``.bak-*`` files.

    ``keep_trust=False`` for content someone else wrote (an imported bundle): a project config written that way is
    left untrusted until the user approves it with `k3code trust`.
    """
    # the user config's directory is $K3CODE_HOME (0700, issue #163: onboarding creates it here); a project's
    # .k3code/ is part of the repository and keeps the umask's mode
    ensure_state_dir(path.parent)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    bak: Path | None = None
    with locked(path):
        if backup and path.is_file():
            previous = path.read_bytes()
            if previous == text.encode("utf-8"):
                return None
            bak = backup_path(path)
            bak.write_bytes(previous)
        if keep_trust:
            with trust.keeping_trust(path):
                atomic_write_text(path, text)
        else:
            atomic_write_text(path, text)
    return bak


def validate(data: dict[str, Any]) -> None:
    """Raise :class:`ConfigError` unless ``data`` is a valid config mapping."""
    try:
        Settings(**data)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise ConfigError(f"{loc}: {first['msg']}") from e
    if data.get("reliability"):
        _validate_reliability(data["reliability"])
    if data.get("permissions"):
        _validate_permissions(data["permissions"])


def _validate_reliability(rel: Any) -> None:
    from k3code.reliability import ReliabilityFlags, ReliabilitySettings

    if not isinstance(rel, dict):
        raise ConfigError("reliability: must be a mapping")
    known = set(ReliabilitySettings.__dataclass_fields__)
    for key in rel:
        if key not in known:
            raise ConfigError(f"reliability.{key}: unknown key (known: {', '.join(sorted(known))})")
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
