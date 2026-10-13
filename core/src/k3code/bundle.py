"""``.k3bundle``: tar.gz with manifest.json, redacted settings/ and sessions/<id>.json."""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from k3code import __version__ as k3_version
from k3code import confio, trust
from k3code.gateway.sessions import SessionStore, StoredSession
from k3code.paths import project_config_path, user_config_path
from k3code.redact import REDACTED, drop_values, redact, redact_session_json

BUNDLE_VERSION = 1
MAX_MEMBER_BYTES = 200 * 1024 * 1024
_SETTINGS_FILES = {"settings/user.config.yaml": "user", "settings/project.config.yaml": "project"}
_SESSION_RE = re.compile(r"^sessions/([A-Za-z0-9_\-]{1,64})\.json$")
#: Session meta an imported bundle must not carry over: a permission mode (yolo), extra writable roots, and the
#: background / automation-origin markers that make the daemon run or resume the session on its own, and an armed
#: ``ultra_mode`` (ultracode spawns parallel sub-agents on the next prompt, which the importer never chose), and the
#: goal: an active one is resumed by the daemon's boot resume and watchdog, which would run the bundle author's goal
#: text and ``--check`` command unattended, and ``agent_limits`` (a /maxagents raise: more sub-agents, more spend).
_UNTRUSTED_META = frozenset({"mode", "add_dirs", "background", "ultra_mode", "goal", "agent_limits"})


class BundleError(ValueError):
    pass


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def session_to_dict(s: StoredSession, secrets: list[str] | None = None) -> dict[str, Any]:
    """Known secret values first: the shape pass can cut one at a `,` or a quote and leave its tail behind."""
    return redact_session_json(drop_values(asdict(s), secrets or []))


def _known_secrets(config: Any, cwd: Path) -> list[str]:
    """Every secret value this process knows (configured provider keys, secret-named env vars), longest first."""
    from k3code.debugdump import secret_values

    if config is None:
        from k3code.config import load_config

        try:
            config = load_config(project_dir=cwd)
        except Exception:  # noqa: BLE001 - a config that does not load still leaves the env vars to check
            config = None
    return secret_values(config)


def write_bundle(
    path: Path,
    *,
    store: SessionStore,
    cwd: Path,
    session_ids: list[str],
    settings: bool = True,
    config: Any = None,
) -> dict[str, Any]:
    """Write the bundle; returns the manifest. ``config`` names the keys to drop (default: loaded for ``cwd``)."""
    secrets = _known_secrets(config, cwd)
    files: dict[str, bytes] = {}
    if settings:
        for name, kind in _SETTINGS_FILES.items():
            src = user_config_path() if kind == "user" else project_config_path(cwd)
            if src.is_file():
                data = redact(drop_values(confio.read_yaml(src), secrets))
                files[name] = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()
    exported: list[str] = []
    for sid in session_ids:
        stored = store.get(sid)
        if stored is None:
            raise BundleError(f"unknown session: {sid}")
        session = session_to_dict(stored, secrets)
        files[f"sessions/{sid}.json"] = json.dumps(session, ensure_ascii=False, indent=1).encode()
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
    """Read a bundle. Anything wrong with the file (truncated gzip, a manifest that is not UTF-8 or not an object,
    settings that are not YAML) is a BundleError: the importers catch that and nothing else."""
    try:
        return _read_bundle(Path(path))
    except BundleError:
        raise
    except (tarfile.TarError, EOFError, OSError, zlib.error, ValueError, yaml.YAMLError) as e:
        raise BundleError(f"not a valid .k3bundle: {e}") from e


def _read_bundle(path: Path) -> Bundle:
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
        if not isinstance(manifest, dict):
            raise BundleError("bad manifest.json: not an object")
        if manifest.get("version") != BUNDLE_VERSION:
            raise BundleError(f"unsupported bundle version: {manifest.get('version')}")
        out = Bundle(manifest)
        for name in members:  # only whitelisted names are ever read; nothing is extracted to disk
            if name in _SETTINGS_FILES:
                try:
                    data = yaml.safe_load(load(name)) or {}
                except yaml.YAMLError as e:
                    raise BundleError(f"bad {name}: not valid YAML") from e
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
    """Drop ``<redacted>`` values from the dicts inside ``obj``. A list keeps the marker in place: without it every
    later argument of ``--model <redacted> --port 9000`` would shift (``--model --port 9000``)."""
    if isinstance(obj, dict):
        return {k: _strip_redacted(v) for k, v in obj.items() if v != REDACTED}
    if isinstance(obj, list):
        return [_strip_redacted(v) for v in obj]
    return obj


def _has_redacted(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any(v == REDACTED or _has_redacted(v) for v in obj.values())
    if isinstance(obj, list):
        return any(v == REDACTED or _has_redacted(v) for v in obj)
    return False


def _merge_list(existing: list[Any], incoming: list[Any]) -> list[Any]:
    """An incoming list holding placeholders, over a list of the same length (the bundle's own export, imported on the
    machine it came from): each placeholder keeps what is there, so a round trip does not delete ``--token=…`` from an
    ``args`` list or the ``command`` of a hook. Lists of another length are not aligned: placeholders are dropped."""
    if len(existing) != len(incoming):
        return _strip_redacted(incoming)
    out: list[Any] = []
    for old, new in zip(existing, incoming, strict=True):
        if new == REDACTED:
            out.append(old)
        elif isinstance(new, dict) and isinstance(old, dict):
            out.append(merge_settings(old, new))
        elif isinstance(new, list) and isinstance(old, list) and _has_redacted(new):
            out.append(_merge_list(old, new))
        else:
            out.append(_strip_redacted(new))
    return out


def merge_settings(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``incoming`` over ``existing``; ``<redacted>`` never overwrites (or creates) a value."""
    out = dict(existing)
    for k, v in incoming.items():
        if v == REDACTED:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_settings(out[k], v)
        elif isinstance(v, list) and _has_redacted(v) and isinstance(out.get(k), list):
            out[k] = _merge_list(out[k], v)
        elif isinstance(v, dict | list):
            out[k] = _strip_redacted(v)
        else:
            out[k] = v
    return out


@dataclass
class SensitiveItem:
    """One user-level setting from a bundle that runs code, approves tools or sends keys somewhere."""

    key: str  # e.g. "mcp.servers.github", "permissions.bash", "providers", "hooks"
    path: tuple[str, ...]  # where it sits in the incoming settings
    diff: list[str]  # "old → new" lines, secrets redacted

    def text(self) -> str:
        return f"The bundle changes {self.key}:\n" + "\n".join(f"  {line}" for line in self.diff)


#: Single settings that approve tools, widen reach or send a key somewhere, gated like the sections below.
_SCALAR_SENSITIVE: tuple[tuple[str, ...], ...] = (
    ("permission_mode",),
    ("headless_permission",),
    ("autonomy", "auto_do_plans"),
    ("autonomy", "auto_do_projects"),
    ("mem0", "url"),
    ("mem0", "api_key_env"),
    ("skills", "roots"),
    ("web", "allow_private"),
    ("browser", "cdp_url"),
    ("browser", "executable_path"),  # a program the browser tools launch
    ("research", "searxng_url"),  # its origin is exempt from the private-address guard
    ("automation", "webhook_port"),  # opens a listening port
    ("artifacts", "publish_dir"),  # where /artifacts publish writes
)


def _at(settings: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = settings
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _show(value: Any) -> str:
    if value is None:
        return "(not set)"
    return json.dumps(redact(value), ensure_ascii=False, sort_keys=True)


def sensitive_items(bundle: Bundle, existing: dict[str, Any] | None = None) -> list[SensitiveItem]:
    """The user-level settings an import must not apply without an explicit yes, item by item.

    MCP servers (a stdio ``command`` runs on the next start), permission rules (approve tools without asking),
    providers (``base_url``/``api_key_env``: where a key is sent) and ``hooks``. ``existing`` defaults to the
    user's config.yaml; an item that would not change anything is not listed.
    """
    incoming = bundle.settings.get("user") or {}
    have = confio.read_yaml(user_config_path()) if existing is None else existing
    items: list[SensitiveItem] = []
    servers = (incoming.get("mcp") or {}).get("servers") if isinstance(incoming.get("mcp"), dict) else None
    old_servers = (have.get("mcp") or {}).get("servers") or {} if isinstance(have.get("mcp"), dict) else {}
    for name, spec in servers.items() if isinstance(servers, dict) else []:
        old = old_servers.get(name) if isinstance(old_servers, dict) else None
        if spec != old and spec != REDACTED:
            diff = [f"{k}: {_show((old or {}).get(k))} → {_show(v)}" for k, v in (spec or {}).items()]
            items.append(SensitiveItem(f"mcp.servers.{name}", ("mcp", "servers", name), diff or [_show(spec)]))
    perms = incoming.get("permissions")
    old_perms = have.get("permissions") if isinstance(have.get("permissions"), dict) else {}
    for tool, entry in perms.items() if isinstance(perms, dict) else []:
        if entry != old_perms.get(tool):
            diff = [f"{_show(old_perms.get(tool))} → {_show(entry)}"]
            items.append(SensitiveItem(f"permissions.{tool}", ("permissions", str(tool)), diff))
    providers = incoming.get("providers")
    if isinstance(providers, list):
        old_by_name = {p.get("name"): p for p in have.get("providers") or [] if isinstance(p, dict)}
        diff = []
        for p in (p for p in providers if isinstance(p, dict)):
            old = old_by_name.get(p.get("name")) or {}
            for k in ("base_url", "api_key_env"):
                if p.get(k) != old.get(k):
                    diff.append(f"providers.{p.get('name', '?')}.{k}: {_show(old.get(k))} → {_show(p.get(k))}")
        if diff:
            items.append(SensitiveItem("providers", ("providers",), diff))
    for path in _SCALAR_SENSITIVE:
        new, old = _at(incoming, path), _at(have, path)
        if new is not None and new != REDACTED and new != old:
            items.append(SensitiveItem(".".join(path), path, [f"{_show(old)} → {_show(new)}"]))
    if "hooks" in incoming and incoming["hooks"] != have.get("hooks"):
        items.append(SensitiveItem("hooks", ("hooks",), [f"{_show(have.get('hooks'))} → {_show(incoming['hooks'])}"]))
    return items


def _without(settings: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """A copy of ``settings`` with the value at ``path`` removed (and any section that leaves empty)."""
    out = dict(settings)
    head, *rest = path
    if not rest:
        out.pop(head, None)
    elif isinstance(out.get(head), dict):
        out[head] = _without(out[head], tuple(rest))
        if not out[head]:
            out.pop(head)
    return out


@dataclass
class ImportReport:
    settings_written: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    sessions: dict[str, str] = field(default_factory=dict)  # original id → imported id
    #: the project config the bundle wrote; it is not trusted until the user runs `k3code trust`
    untrusted_project: str | None = None
    #: sensitive user settings left out (no explicit yes for each, see sensitive_items)
    skipped: list[str] = field(default_factory=list)
    #: sessions left out because their record is not a session (original id: why)
    invalid_sessions: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        lines = []
        for f in self.settings_written:
            lines.append(f"settings written: {f}")
        for key in self.skipped:
            lines.append(
                f"warning: skipped {key} from the bundle (runs code, approves tools or sends keys; accept it item by "
                "item in an interactive import, or pass --trust-bundle)"
            )
        for sid, why in self.invalid_sessions.items():
            lines.append(f"warning: skipped session {sid} from the bundle: {why}")
        for b in self.backups:
            lines.append(f"backup: {b}")
        for old, new in self.sessions.items():
            lines.append(f"session {old}" + (f" → {new} (id collided)" if old != new else " imported"))
        if self.untrusted_project:
            lines.append(
                f"{self.untrusted_project} is not trusted yet: `k3code trust` shows what it changes and applies it"
            )
        return "\n".join(lines) or "nothing imported"


def _usable_cwd(value: Any) -> bool:
    try:
        path = Path(str(value))
        return bool(value) and path.is_absolute() and path.is_dir() and path != Path(path.anchor)
    except (OSError, ValueError):
        return False


def _stored_from(obj: dict[str, Any]) -> tuple[StoredSession | None, str]:
    """The session a bundle record describes, or ``(None, why)`` when it cannot be one. Text fields must be text and
    ``messages`` a list of well-formed message objects (rejected otherwise); ``usage``, ``meta`` and the timestamps of
    the wrong type are dropped (an empty value is used)."""
    for key in ("title", "model", "provider", "cwd"):
        if key in obj and not isinstance(obj[key], str):
            return None, f"{key} is not text"
    messages = obj.get("messages", [])
    if not isinstance(messages, list):
        return None, "messages is not a list"
    for m in messages:
        calls = m.get("tool_calls") if isinstance(m, dict) else None
        if (
            not isinstance(m, dict)
            or not isinstance(m.get("role"), str)
            or not (calls is None or (isinstance(calls, list) and all(isinstance(c, dict) for c in calls)))
        ):
            return None, "messages holds an entry that is not a message"
    sess = StoredSession(
        session_id=str(obj["session_id"]),
        title=obj.get("title", ""),
        model=obj.get("model", ""),
        provider=obj.get("provider", ""),
        cwd=obj.get("cwd", ""),
        messages=messages,
        usage=obj["usage"] if isinstance(obj.get("usage"), dict) else {},
        meta=obj["meta"] if isinstance(obj.get("meta"), dict) else {},
    )
    for key in ("created_at", "updated_at"):
        value = obj.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            setattr(sess, key, float(value))
    return sess, ""


def apply_bundle(
    bundle: Bundle,
    *,
    store: SessionStore,
    cwd: Path,
    settings: bool = True,
    sessions: bool = True,
    accept: Callable[[SensitiveItem], bool] | None = None,
    trust_bundle: bool = False,
) -> ImportReport:
    """Merge the bundle. Each sensitive user setting (sensitive_items) is applied only with ``trust_bundle`` or
    when ``accept(item)`` says yes; without either it is skipped and the report says so."""
    rep = ImportReport()
    if settings:
        for kind, incoming in bundle.settings.items():
            target = user_config_path() if kind == "user" else project_config_path(cwd)
            if kind == "user" and not trust_bundle:
                for item in sensitive_items(bundle, confio.read_yaml(target)):
                    if accept is None or not accept(item):
                        incoming = _without(incoming, item.path)
                        rep.skipped.append(item.key)
            merged = merge_settings(confio.read_yaml(target), incoming)
            confio.validate(merged)
            # the bundle's project settings are its author's: never trusted on the user's behalf (see k3code.trust)
            bak = confio.write_yaml(target, merged, keep_trust=kind == "user")
            rep.settings_written.append(str(target))
            if kind != "user" and trust.decision(cwd) not in (trust.TRUSTED, trust.NONE):
                rep.untrusted_project = str(target)
            if bak:
                rep.backups.append(str(bak))
    if sessions:
        for obj in bundle.sessions:
            sess, why = _stored_from(obj)
            if sess is None:  # one bad record broke session.list / session.resume for the whole store
                rep.invalid_sessions[str(obj.get("session_id"))] = why
                continue
            sess.meta = {k: v for k, v in sess.meta.items() if k not in _UNTRUSTED_META and not k.startswith("origin")}
            if not _usable_cwd(sess.cwd):  # another machine's path, or "/": the session works where it is imported
                sess.cwd = str(cwd)
            old = sess.session_id
            if store.get(old) is not None:
                sess.session_id = store.new_id()
            store.insert(sess)
            rep.sessions[old] = sess.session_id
    return rep
