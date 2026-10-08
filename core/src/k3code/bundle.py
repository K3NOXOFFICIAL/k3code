"""``.k3bundle``: tar.gz with manifest.json, redacted settings/ and sessions/<id>.json."""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from k3code import __version__ as k3_version
from k3code import confio
from k3code.gateway.sessions import SessionStore, StoredSession
from k3code.paths import project_config_path, user_config_path
from k3code.redact import REDACTED, redact, redact_session_json

BUNDLE_VERSION = 1
MAX_MEMBER_BYTES = 200 * 1024 * 1024
_SETTINGS_FILES = {"settings/user.config.yaml": "user", "settings/project.config.yaml": "project"}
_SESSION_RE = re.compile(r"^sessions/([A-Za-z0-9_\-]{1,64})\.json$")
#: Session meta an imported bundle must not carry over: a permission mode (yolo), extra writable roots, and the
#: background / automation-origin markers that make the daemon run or resume the session on its own.
_UNTRUSTED_META = frozenset({"mode", "add_dirs", "background"})


class BundleError(ValueError):
    pass


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def session_to_dict(s: StoredSession) -> dict[str, Any]:
    return redact_session_json(asdict(s))


def write_bundle(
    path: Path,
    *,
    store: SessionStore,
    cwd: Path,
    session_ids: list[str],
    settings: bool = True,
) -> dict[str, Any]:
    """Write the bundle; returns the manifest."""
    files: dict[str, bytes] = {}
    if settings:
        for name, kind in _SETTINGS_FILES.items():
            src = user_config_path() if kind == "user" else project_config_path(cwd)
            if src.is_file():
                data = redact(confio.read_yaml(src))
                files[name] = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()
    exported: list[str] = []
    for sid in session_ids:
        stored = store.get(sid)
        if stored is None:
            raise BundleError(f"unknown session: {sid}")
        files[f"sessions/{sid}.json"] = json.dumps(session_to_dict(stored), ensure_ascii=False, indent=1).encode()
        exported.append(sid)
    manifest = {
        "version": BUNDLE_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "k3code": k3_version,
        "redacted": True,
        "contents": {"settings": sorted(n for n in files if n.startswith("settings/")), "sessions": exported},
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        _add(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
        for name, data in files.items():
            _add(tar, name, data)
    path.chmod(0o600)
    return manifest


@dataclass
class Bundle:
    manifest: dict[str, Any]
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)  # "user"|"project" → config
    sessions: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"k3bundle v{self.manifest.get('version')} created {self.manifest.get('created')}"]
        lines.append("settings: " + (", ".join(sorted(self.settings)) or "(none)"))
        lines.append(f"sessions: {len(self.sessions)}")
        for s in self.sessions[:10]:
            title = s.get("title") or "(untitled)"
            lines.append(f"  - {s.get('session_id')}  {title}  ({len(s.get('messages', []))} msgs)")
        return "\n".join(lines)


def read_bundle(path: Path) -> Bundle:
    path = Path(path)
    if not path.is_file():
        raise BundleError(f"no such bundle: {path}")
    try:
        tar = tarfile.open(path, "r:gz")  # noqa: SIM115 - closed by the with below
    except (tarfile.TarError, OSError) as e:
        raise BundleError(f"not a valid .k3bundle: {e}") from e
    with tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        if "manifest.json" not in members:
            raise BundleError("bundle has no manifest.json")

        def load(name: str) -> bytes:
            m = members[name]
            if m.size > MAX_MEMBER_BYTES:
                raise BundleError(f"{name} too large")
            f = tar.extractfile(m)
            assert f is not None
            return f.read()

        try:
            manifest = json.loads(load("manifest.json"))
        except json.JSONDecodeError as e:
            raise BundleError(f"bad manifest.json: {e}") from e
        if manifest.get("version") != BUNDLE_VERSION:
            raise BundleError(f"unsupported bundle version: {manifest.get('version')}")
        out = Bundle(manifest)
        for name in members:  # only whitelisted names are ever read; nothing is extracted to disk
            if name in _SETTINGS_FILES:
                data = yaml.safe_load(load(name)) or {}
                if isinstance(data, dict):
                    out.settings[_SETTINGS_FILES[name]] = data
            elif _SESSION_RE.match(name):
                try:
                    obj = json.loads(load(name))
                except json.JSONDecodeError as e:
                    raise BundleError(f"bad {name}: {e}") from e
                if isinstance(obj, dict) and obj.get("session_id"):
                    out.sessions.append(obj)
    return out


def _strip_redacted(obj: Any) -> Any:
    """Drop ``<redacted>`` placeholders anywhere inside ``obj`` (they must never be written to config)."""
    if isinstance(obj, dict):
        return {k: _strip_redacted(v) for k, v in obj.items() if v != REDACTED}
    if isinstance(obj, list):
        return [_strip_redacted(v) for v in obj if v != REDACTED]
    return obj


def merge_settings(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``incoming`` over ``existing``; ``<redacted>`` never overwrites (or creates) a value."""
    out = dict(existing)
    for k, v in incoming.items():
        if v == REDACTED:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_settings(out[k], v)
        elif isinstance(v, dict | list):
            out[k] = _strip_redacted(v)
        else:
            out[k] = v
    return out


@dataclass
class ImportReport:
    settings_written: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    sessions: dict[str, str] = field(default_factory=dict)  # original id → imported id

    def describe(self) -> str:
        lines = []
        for f in self.settings_written:
            lines.append(f"settings written: {f}")
        for b in self.backups:
            lines.append(f"backup: {b}")
        for old, new in self.sessions.items():
            lines.append(f"session {old}" + (f" → {new} (id collided)" if old != new else " imported"))
        return "\n".join(lines) or "nothing imported"


def apply_bundle(
    bundle: Bundle,
    *,
    store: SessionStore,
    cwd: Path,
    settings: bool = True,
    sessions: bool = True,
) -> ImportReport:
    rep = ImportReport()
    if settings:
        for kind, incoming in bundle.settings.items():
            target = user_config_path() if kind == "user" else project_config_path(cwd)
            merged = merge_settings(confio.read_yaml(target), incoming)
            confio.validate(merged)
            bak = confio.write_yaml(target, merged)
            rep.settings_written.append(str(target))
            if bak:
                rep.backups.append(str(bak))
    if sessions:
        for obj in bundle.sessions:
            sess = StoredSession(
                **{k: obj[k] for k in StoredSession.__dataclass_fields__ if k in obj and k != "session_id"},
                session_id=str(obj["session_id"]),
            )
            sess.meta = {
                k: v for k, v in (sess.meta if isinstance(sess.meta, dict) else {}).items()
                if k not in _UNTRUSTED_META and not k.startswith("origin")
            }
            old = sess.session_id
            if store.get(old) is not None:
                sess.session_id = store.new_id()
            store.insert(sess)
            rep.sessions[old] = sess.session_id
    return rep
