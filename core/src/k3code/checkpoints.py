"""File checkpoints for /undo (and /rewind): what a file held before the agent's ``write``/``edit`` changed it.

Before the first change to a file in a turn, its bytes (or "did not exist") are kept, keyed by (session, turn, real
path); after every change, the hash of what the agent left there. A turn is the index its messages start at in the
stored conversation, the same number an /undo cuts the conversation at, so ``restore(session, index)`` rewinds every
file the turns from ``index`` on changed: the old bytes go back, a file the agent created is removed. A file whose
content is no longer what the agent last wrote (the user's editor, a formatter, a shell command) is a conflict: left
as it is and listed. Shell commands' own changes to files are not recorded (out of scope; /undo says so).

Private state: ``$K3CODE_HOME/checkpoints/<session>/`` (0700), files 0600 whatever the umask. Retention is capped
(``MAX_TURNS`` per session, ``MAX_FILE_BYTES`` per file, ``MAX_SESSION_BYTES`` per session): oldest turns go first,
and a file that does not fit is not kept, with a notice the first time in a turn, never silently.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code import paths
from k3code.tools.safe_open import open_regular

MAX_TURNS = 50
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_SESSION_BYTES = 100 * 1024 * 1024

_INDEX = "index.json"
_SAFE_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")
#: the tools run on worker threads, a session's turns one after another but its calls maybe side by side
_LOCK = threading.Lock()


@dataclasses.dataclass(frozen=True)
class Prior:
    """A file's state just before a tool changes it: ``path`` real, ``data`` None when it did not exist."""

    path: str
    data: bytes | None
    skipped: str = ""  # why its bytes are not kept (then ``data`` is None too)


@dataclasses.dataclass
class Rewind:
    """What ``restore`` did, by real path; conflicts and skipped files carry why they were left alone."""

    restored: list[str] = dataclasses.field(default_factory=list)
    deleted: list[str] = dataclasses.field(default_factory=list)
    conflicts: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    skipped: list[tuple[str, str]] = dataclasses.field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.restored or self.deleted or self.conflicts or self.skipped)

    def report(self, *, shell: bool = False, delegated: bool = False) -> dict[str, Any]:
        """The /undo result's file fields, ``note`` the text the client shows."""
        lines: list[str] = []
        if self.restored:
            lines += [f"restored {len(self.restored)} file(s):", *(f"  {p}" for p in self.restored)]
        if self.deleted:
            lines += [f"removed {len(self.deleted)} file(s) the agent created:", *(f"  {p}" for p in self.deleted)]
        if self.conflicts:
            lines += [
                f"left {len(self.conflicts)} file(s) untouched (conflict):",
                *(f"  {p}: {why}" for p, why in self.conflicts),
            ]
        if self.skipped:
            lines += [f"not restored {len(self.skipped)} file(s):", *(f"  {p}: {why}" for p, why in self.skipped)]
        if shell:
            lines.append("shell commands ran in the undone turn(s): what they changed on disk was not restored")
        if delegated:
            lines.append("sub-agents ran in the undone turn(s): the files they changed were not restored")
        return {
            "restored": list(self.restored),
            "deleted": list(self.deleted),
            "conflicts": [{"path": p, "reason": why} for p, why in self.conflicts],
            "skipped": [{"path": p, "reason": why} for p, why in self.skipped],
            "note": "\n".join(lines),
        }


def _session_dir(session_id: str) -> Path | None:
    if not _SAFE_ID.fullmatch(session_id or ""):
        return None  # never a path of its own: the id names one directory under checkpoints/
    return paths.home() / "checkpoints" / session_id


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: str, limit: int | None = None, *, exact: bool = False) -> bytes | None:
    """``path``'s bytes (at most ``limit`` + 1), None when it does not exist. A link on the path is resolved by
    safe_open, never followed blindly; ``exact``: refused (NotAllowed) unless it leads to ``path`` itself."""
    try:
        with open_regular(path, guard=(lambda real: real == path) if exact else None) as f:
            return f.read() if limit is None else f.read(limit + 1)
    except FileNotFoundError:
        return None


def _private_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    finally:
        if fd != -1:
            os.close(fd)
    os.replace(tmp, path)


def _load(root: Path) -> dict[str, Any]:
    try:
        index = json.loads((root / _INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"turns": {}}
    return index if isinstance(index, dict) and isinstance(index.get("turns"), dict) else {"turns": {}}


def _save(root: Path, index: dict[str, Any]) -> None:
    _private_write(root / _INDEX, json.dumps(index).encode("utf-8"))


def _evict(root: Path, index: dict[str, Any], key: str) -> None:
    index["turns"].pop(key, None)
    shutil.rmtree(root / key, ignore_errors=True)


def _turn_keys(index: dict[str, Any]) -> list[str]:
    return sorted(index["turns"], key=int)


def _stored_bytes(index: dict[str, Any]) -> int:
    return sum(int(e.get("size") or 0) for t in index["turns"].values() for e in t["files"].values())


def capture(path: str | os.PathLike[str]) -> Prior:
    """The state of ``path`` before a tool writes it (on a worker thread: up to ``MAX_FILE_BYTES`` are read)."""
    real = os.path.realpath(path)
    try:
        data = _read(real, MAX_FILE_BYTES)
    except OSError as e:
        return Prior(real, None, skipped=f"could not be read ({e.strerror or e})")
    if data is not None and len(data) > MAX_FILE_BYTES:
        return Prior(real, None, skipped=f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    return Prior(real, data)


@dataclasses.dataclass(frozen=True)
class Turn:
    """One turn of a session: the index its messages start at in the stored conversation."""

    session_id: str
    index: int

    def record(self, prior: Prior) -> str | None:
        """Keep ``prior`` as the file's state before this turn (the first change in a turn wins) and the hash of what
        is on disk now as what the agent last wrote. Returns a notice when the file's bytes could not be kept: once
        per file and turn. Never raises: the tool's change is made, a checkpoint that fails is a notice."""
        try:
            skipped = self._record(prior)
        except OSError as e:
            skipped = f"the checkpoint could not be written ({e.strerror or e})"
        except Exception as e:  # noqa: BLE001 - a path that is not UTF-8, a directory owned by someone else ...
            skipped = f"the checkpoint could not be written ({type(e).__name__})"
        if skipped:
            return f"{prior.path} is not checkpointed ({skipped}): /undo will not restore it"
        return None

    def _record(self, prior: Prior) -> str:
        root = _session_dir(self.session_id)
        if root is None:
            return ""
        try:
            after = _read(prior.path)
        except OSError:
            after = None
        with _LOCK:
            paths.ensure_private_dir(root)
            index = _load(root)
            key = str(self.index)
            for old in _turn_keys(index):
                if int(old) > self.index:  # the conversation was cut below that turn: it can no longer be undone
                    _evict(root, index, old)
            files = index["turns"].setdefault(key, {"files": {}})["files"]
            if prior.path in files:
                files[prior.path]["after"] = None if after is None else _sha(after)
                _save(root, index)
                return ""
            entry: dict[str, Any] = {"after": None if after is None else _sha(after)}
            skipped = prior.skipped
            size = len(prior.data or b"")
            if not skipped:
                for old in _turn_keys(index):
                    if old == key or _stored_bytes(index) + size <= MAX_SESSION_BYTES:
                        break
                    _evict(root, index, old)
                if _stored_bytes(index) + size > MAX_SESSION_BYTES:
                    skipped = f"the session's checkpoints are full ({MAX_SESSION_BYTES // (1024 * 1024)} MB)"
            if skipped:
                entry["skipped"] = skipped
            elif prior.data is None:
                entry["blob"] = None  # did not exist: undo removes it
            else:
                blob = hashlib.sha256(prior.path.encode()).hexdigest()[:32] + ".bin"
                paths.ensure_private_dir(root / key)
                _private_write(root / key / blob, prior.data)
                entry.update(blob=blob, size=size)
            files[prior.path] = entry
            for old in _turn_keys(index)[: max(0, len(index["turns"]) - MAX_TURNS)]:
                if old != key:
                    _evict(root, index, old)
            _save(root, index)
        return skipped


def _inside(path: str, roots: list[str]) -> bool:
    for root in roots:
        real = os.path.realpath(root)
        if path == real or path.startswith(real.rstrip("/") + "/"):
            return True
    return False


def _rewind_file(path: str, first: dict[str, Any], after: str | None, blobs: Path, roots: list[str]) -> str:
    """Put ``path`` back as ``first`` recorded it; returns "restored", "deleted", "" (nothing to do), or raises
    ValueError with the conflict."""
    from k3code.tools import _atomic_write

    if os.path.realpath(path) != path:
        raise ValueError("the path leads somewhere else now (a link was put on it)")
    if not _inside(path, roots):
        raise ValueError("outside the session's project roots")
    current = _read(path, exact=True)
    if current is None or after is None or _sha(current) != after:
        raise ValueError("changed since the agent wrote it")
    if first.get("blob") is None:
        os.unlink(path)
        return "deleted"
    data = (blobs / str(first["blob"])).read_bytes()
    if data == current:
        return ""
    _atomic_write(Path(path), data)
    return "restored"


def _salvage(root: Path, key: str, entry: dict[str, Any], path: str) -> str | None:
    """Copy the recorded earlier bytes of ``path`` to ``<session dir>/unrestored/`` when writing them back failed
    (permissions, a full disk); the path of the copy, or None when there is nothing to keep."""
    blob = entry.get("blob")
    if not blob:
        return None
    where = hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:8]  # same-named files in two folders
    dest = root / "unrestored" / f"{os.path.basename(path)}.{where}.{key}.bin"
    try:
        paths.ensure_private_dir(dest.parent)
        _private_write(dest, (root / key / str(blob)).read_bytes())
    except OSError:
        return None
    return str(dest)


def restore(session_id: str, index: int, roots: list[str]) -> Rewind:
    """Rewind every file the session's turns from ``index`` on changed, inside ``roots`` (the session's cwd and
    add-dirs); those turns' checkpoints are gone afterwards (their messages are)."""
    out = Rewind()
    root = _session_dir(session_id)
    if root is None or not (root / _INDEX).exists():
        return out
    with _LOCK:
        stored = _load(root)
        keys = [k for k in _turn_keys(stored) if int(k) >= index]
        first: dict[str, tuple[str, dict[str, Any]]] = {}  # path -> (turn, its state before the earliest turn)
        last: dict[str, str | None] = {}  # path -> hash of what the agent last wrote
        for key in keys:
            for path, entry in stored["turns"][key]["files"].items():
                first.setdefault(path, (key, entry))
                last[path] = entry.get("after")
        for path, (key, entry) in first.items():
            if entry.get("skipped"):
                out.skipped.append((path, str(entry["skipped"])))
                continue
            try:
                done = _rewind_file(path, entry, last[path], root / key, roots)
            except ValueError as e:
                out.conflicts.append((path, str(e)))
                continue
            except OSError as e:
                # the turn's checkpoints go next, and with them the only copy of the earlier bytes: keep that one
                kept = _salvage(root, key, entry, path)
                why = e.strerror or str(e)
                out.conflicts.append((path, f"{why}; its earlier version is kept at {kept}" if kept else why))
                continue
            if done == "restored":
                out.restored.append(path)
            elif done == "deleted":
                out.deleted.append(path)
        for key in keys:
            _evict(root, stored, key)
        _save(root, stored)
    return out


def prune(keep: Callable[[str], bool], max_age_s: float, now: float | None = None) -> int:
    """Remove the checkpoints of sessions that ``keep`` does not name (the live ones) and that were last written more
    than ``max_age_s`` seconds ago: a stored session that nobody resumed in that long is not going to be undone. Returns
    how many sessions' checkpoints went."""
    base = paths.home() / "checkpoints"
    if not base.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for d in base.iterdir():
        if d.is_symlink() or not d.is_dir() or keep(d.name):
            continue
        try:
            written = (d / _INDEX).stat().st_mtime if (d / _INDEX).exists() else d.stat().st_mtime
        except OSError:
            continue
        if now - written > max_age_s:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def drop(session_id: str) -> None:
    """Forget the session's checkpoints (a compaction renumbered its messages: no turn index matches any more)."""
    root = _session_dir(session_id)
    if root is not None:
        with _LOCK, contextlib.suppress(OSError):
            shutil.rmtree(root)
