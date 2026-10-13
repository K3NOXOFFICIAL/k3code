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
#: text and ``--check`` command unattended, and ``agent_limits`` (a /maxagents raise: more sub-agents, more spend), and
#: ``file_stamps`` (another machine's paths and times: they could let a first write skip the read-first check).
_UNTRUSTED_META = frozenset({"mode", "add_dirs", "background", "ultra_mode", "goal", "agent_limits", "file_stamps"})


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


def _placeholder(value: Any) -> bool:
    """A string holding the marker: ``<redacted>`` itself or a value ``redact()`` hid a part of (a token-shaped URL
    segment: ``https://gateway.ai.cloudflare.com/v1/<redacted>/gw/openai``). Either way the real value is not in it."""
    return isinstance(value, str) and REDACTED in value


def _hidden_in_part(obj: Any, path: str = "") -> list[str]:
    """Where ``obj`` has a string that holds the marker among other text (``redact()`` hid part of it, or it is the
    user's own text): the exact marker is the exporter's ordinary "hidden" and is not listed."""
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if (_placeholder(k) and k != REDACTED) or (_placeholder(v) and v != REDACTED):
                found.append(here)
            else:
                found.extend(_hidden_in_part(v, here))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            here = f"{path}[{i}]"
            if _placeholder(v) and v != REDACTED:
                found.append(f"{here} (a list entry)")
            else:
                found.extend(_hidden_in_part(v, here))
    return found


def _strip_redacted(obj: Any) -> Any:
    """Drop placeholder values and keys from the dicts inside ``obj``. A list keeps the marker in place: without it
    every later argument of ``--model <redacted> --port 9000`` would shift (``--model --port 9000``)."""
    if isinstance(obj, dict):
        return {k: _strip_redacted(v) for k, v in obj.items() if not _placeholder(k) and not _placeholder(v)}
    if isinstance(obj, list):
        return [_strip_redacted(v) for v in obj]
    return obj


def _has_redacted(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any(_placeholder(k) or _has_redacted(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_redacted(v) for v in obj)
    return _placeholder(obj)


def _names(items: list[Any]) -> list[str] | None:
    """The ``name`` of every entry when all of them are dicts with a distinct, plain name; else None."""
    names = [x.get("name") if isinstance(x, dict) else None for x in items]
    if not items or not all(isinstance(n, str) and n and not _placeholder(n) for n in names):
        return None
    return names if len(set(names)) == len(names) else None  # type: ignore[return-value]


def _merge_list(existing: list[Any], incoming: list[Any]) -> list[Any]:
    """An incoming list holding placeholders, over a list of the same length (the bundle's own export, imported on the
    machine it came from): each placeholder keeps what is there, so a round trip does not delete ``--token=…`` from an
    ``args`` list or the ``command`` of a hook. Lists of another length are not aligned, so
    the placeholder stays where it is (see ``_strip_redacted``): it never overwrites a real value, and it does not shift
    the arguments after it."""
    old_names, new_names = _names(existing), _names(incoming)
    if old_names is not None and new_names is not None:
        # entries that carry a name (the providers) are matched by it: matched by position, a bundle that lists the same
        # providers in another order paired one provider's key with another's endpoint, and `sensitive_items` (which
        # compares by name) had nothing to show for it
        have = dict(zip(old_names, existing, strict=True))
        return [
            merge_settings(have[name], new) if name in have else _strip_redacted(new)
            for name, new in zip(new_names, incoming, strict=True)
        ]
    if any(isinstance(x, dict) and "name" in x for x in (*existing, *incoming)):
        return _strip_redacted(incoming)  # names that are missing, repeated or hidden: nothing to match them by
    if len(existing) != len(incoming):
        return _strip_redacted(incoming)
    out: list[Any] = []
    for old, new in zip(existing, incoming, strict=True):
        if _placeholder(new):
            out.append(old)
        elif isinstance(new, dict) and isinstance(old, dict):
            out.append(merge_settings(old, new))
        elif isinstance(new, list) and isinstance(old, list) and _has_redacted(new):
            out.append(_merge_list(old, new))
        else:
            out.append(_strip_redacted(new))
    return out


def merge_settings(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``incoming`` over ``existing``; a placeholder (``<redacted>``, or a string it is part of) never
    overwrites or creates a value, and a key holding it is skipped."""
    out = dict(existing)
    for k, v in incoming.items():
        if _placeholder(k) or _placeholder(v):
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
    ("sandbox", "home_readonly"),  # $HOME entries (.kube, .docker, .config/gh ...) shown read-only to sandboxed bash
    ("autonomy", "unattended_network"),  # network for unattended bash: with the line above, a way to send them out
    ("autonomy", "gate_modes"),  # [] switches the scope gate off
    ("autonomy", "fanout", "test_command"),  # a shell command the fan-out runs
)


#: What an unset setting of `_SCALAR_SENSITIVE` stands for (the defaults of k3code.autonomy.DEFAULTS).
_SCALAR_DEFAULTS: dict[tuple[str, ...], Any] = {
    ("autonomy", "unattended_network"): False,
    ("autonomy", "gate_modes"): ["auto"],
    ("autonomy", "fanout", "test_command"): "",
}


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


def sensitive_items(
    bundle: Bundle, existing: dict[str, Any] | None = None, *, incoming: dict[str, Any] | None = None
) -> list[SensitiveItem]:
    """The user-level settings an import must not apply without an explicit yes, item by item.

    MCP servers (a stdio ``command`` runs on the next start), permission rules (approve tools without asking),
    providers (``base_url``/``api_key_env``: where a key is sent) and ``hooks``. ``existing`` defaults to the
    user's config.yaml; an item that would not change anything is not listed.
    """
    if incoming is None:  # the bundle's user settings; its project settings when they land in the user's file
        incoming = bundle.settings.get("user") or {}
    have = confio.read_yaml(user_config_path()) if existing is None else existing
    items: list[SensitiveItem] = []
    servers = (incoming.get("mcp") or {}).get("servers") if isinstance(incoming.get("mcp"), dict) else None
    old_servers = (have.get("mcp") or {}).get("servers") or {} if isinstance(have.get("mcp"), dict) else {}
    for name, spec in servers.items() if isinstance(servers, dict) else []:
        old = old_servers.get(name) if isinstance(old_servers, dict) else None
        if spec != old and not _placeholder(spec):
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
            for k in ("base_url", "api_key_env", "kind"):
                if p.get(k) != old.get(k) and not _placeholder(p.get(k)):  # a hidden value changes nothing on import
                    diff.append(f"providers.{p.get('name', '?')}.{k}: {_show(old.get(k))} → {_show(p.get(k))}")
        # the list is the fallback chain (the first entry that answers is used): dropping or reordering entries sends
        # the prompts somewhere else without a single field of an entry changing
        old_names = [str(p.get("name")) for p in have.get("providers") or [] if isinstance(p, dict)]
        new_names = [str(p.get("name")) for p in providers if isinstance(p, dict)]
        if gone := [n for n in old_names if n not in new_names]:
            diff.append(f"providers: removes {', '.join(gone)}")
        kept_old, kept_new = [n for n in old_names if n in new_names], [n for n in new_names if n in old_names]
        if kept_old != kept_new:
            diff.append(f"providers: order {', '.join(kept_old)} → {', '.join(kept_new)}")
        if diff:
            items.append(SensitiveItem("providers", ("providers",), diff))
    for path in _SCALAR_SENSITIVE:
        new, old = _at(incoming, path), _at(have, path)
        if old is None:  # unset here means the default: a bundle that says the default changes nothing
            old = _SCALAR_DEFAULTS.get(path)
        if new is not None and not _placeholder(new) and new != old:
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
    #: settings the bundle hides in part (a value holding ``<redacted>`` among other text): left as they are on this
    #: machine, or not written; a value that really contains that text is among them
    hidden: list[str] = field(default_factory=list)
    #: providers left out because the bundle hides their endpoint (or key variable) and this machine has none
    incomplete: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = []
        for f in self.settings_written:
            lines.append(f"settings written: {f}")
        for name in self.incomplete:
            lines.append(
                f"warning: provider {name} from the bundle was not imported: the export hid its base_url or "
                "api_key_env and this machine has no entry of that name; add it here (it is not a failed import)"
            )
        for key in self.skipped:
            lines.append(
                f"warning: skipped {key} from the bundle (runs code, approves tools or sends keys; accept it item by "
                "item in an interactive import, or pass --trust-bundle)"
            )
        if self.hidden:
            shown = ", ".join(self.hidden[:5]) + (f" and {len(self.hidden) - 5} more" if len(self.hidden) > 5 else "")
            lines.append(
                "note: the bundle hides all or part of these values; a setting is not written (the value here is "
                "kept); a list entry is written as it stands unless the list here has the same length: "
                f"{shown}"
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


def _drop_incomplete_providers(merged: dict[str, Any], incoming: dict[str, Any]) -> list[str]:
    """Take the providers out of ``merged`` that the bundle brought without an endpoint or key variable (a URL with a
    token-shaped segment is hidden whole by the export, and a hidden value is dropped on import): ``confio.validate``
    refused the entry, and with it the whole import, settings and sessions. An entry this machine already had in that
    shape is not the bundle's doing and stays for validate to name."""
    entries = merged.get("providers")
    brought = {x.get("name") for x in incoming.get("providers") or [] if isinstance(x, dict)}
    if not isinstance(entries, list):
        return []
    dropped: list[str] = []
    kept: list[Any] = []
    for entry in entries:
        gap = (
            isinstance(entry, dict)
            and entry.get("kind") != "claude-cli"
            and not {"base_url", "api_key_env"} <= set(entry)
        )
        if gap and entry.get("name") in brought:
            dropped.append(str(entry.get("name")))
        else:
            kept.append(entry)
    if dropped:
        merged["providers"] = kept
    return dropped


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


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _targets(bundle: Bundle, cwd: Path) -> list[tuple[str, dict[str, Any], Path, bool]]:
    """What :func:`apply_bundle` writes where: ``(kind, settings, file, gated)``. ``gated``: the file is the user's own
    ``config.yaml`` (the user settings always; the project settings too when the working directory is ``$HOME``, where
    the project's config IS the user's), so the sensitive items need a yes. An export made from ``$HOME`` carries the
    same file as its user and its project settings: they are one write, asked about once."""
    user_file, project_file = user_config_path(), project_config_path(cwd)
    same = _same_file(project_file, user_file)
    user, project = bundle.settings.get("user"), bundle.settings.get("project")
    if same and user is not None and project is not None:
        return [("user", merge_settings(user, project), user_file, True)]
    return [
        (kind, incoming, user_file if kind == "user" else project_file, kind == "user" or same)
        for kind, incoming in bundle.settings.items()
    ]


def pending_sensitive(bundle: Bundle, cwd: Path) -> list[SensitiveItem]:
    """The sensitive items :func:`apply_bundle` will ask about (or skip), for a caller that asks before it applies: the
    same items, from the same settings and files, so a yes can be matched to the item it was given for."""
    return [
        item
        for _kind, incoming, target, gated in _targets(bundle, cwd)
        if gated
        for item in sensitive_items(bundle, confio.read_yaml(target), incoming=incoming)
    ]


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
        for kind, incoming, target, gated in _targets(bundle, cwd):
            if gated and not trust_bundle:
                for item in sensitive_items(bundle, confio.read_yaml(target), incoming=incoming):
                    if accept is None or not accept(item):
                        incoming = _without(incoming, item.path)
                        rep.skipped.append(item.key)
            rep.hidden.extend(f"{kind}: {where}" for where in _hidden_in_part(incoming))
            merged = merge_settings(confio.read_yaml(target), incoming)
            rep.incomplete.extend(_drop_incomplete_providers(merged, incoming))
            try:
                confio.validate(merged)
            except confio.ConfigError as e:  # say whose settings: a user and a project file are both in a bundle
                raise confio.ConfigError(f"the bundle's {kind} settings, as merged into {target}: {e}") from e
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
