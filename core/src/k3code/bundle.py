"""``.k3bundle``: tar.gz with manifest.json, redacted settings/ and sessions/<id>.json."""

from __future__ import annotations

import contextlib
import io
import json
import re
import tarfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from k3code import __version__ as k3_version
from k3code import confio, trust
from k3code.gateway.sessions import SessionStore, StoredSession
from k3code.paths import project_config_path, user_config_path
from k3code.redact import _SECRET_SECTIONS, REDACTED, drop_values, is_secret_key, redact, redact_session_json

BUNDLE_VERSION = 1
MAX_MEMBER_BYTES = 200 * 1024 * 1024
_SETTINGS_FILES = {"settings/user.config.yaml": "user", "settings/project.config.yaml": "project"}
_SESSION_RE = re.compile(r"^sessions/([A-Za-z0-9_\-]{1,64})\.json$")
#: Session meta an imported bundle must not carry over: a permission mode (yolo), extra writable roots, and the
#: background / automation-origin markers that make the daemon run or resume the session on its own, and an armed
#: ``ultra_mode`` (ultracode spawns parallel sub-agents on the next prompt, which the importer never chose).
_UNTRUSTED_META = frozenset({"mode", "add_dirs", "background", "ultra_mode"})
_TOKENS_WORD = re.compile(r"tokens(?![a-z])", re.I)


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


def _configs(config: Any, cwd: Path) -> tuple[list[Any], Any]:
    """Where the export learns the secret values: the loaded config (default: loaded for ``cwd``), and the user config
    and the trusted project config as written. A file that does not load still names its secrets, and so does a
    section ``load_config`` drops (a project's ``providers``): with the env vars alone a copy of an MCP value elsewhere
    in the settings would leave in clear (K3NOX-519).

    Also the project config as written whatever its trust state, for the sessions only: a session can quote one never
    trusted, or changed after it was (K3NOX-575). It loads no setting and hides nothing in the user's settings file: a
    file the user never trusted must not hide part of one of their rules."""
    if config is None:
        from k3code.config import load_config

        try:
            config = load_config(project_dir=cwd)
        except Exception:  # noqa: BLE001 - the files as written below still name their secrets
            config = None
    user = _read(user_config_path())
    written = _read(project_config_path(cwd), project=cwd)
    trusted = trust.trusted_text(cwd)
    project = None if trusted is None else _read(project_config_path(cwd), trusted, project=cwd)
    return [config, user, project], written


#: Deepest nesting a project config may have (a real one has 4 or 5 levels).
_MAX_DEPTH = 64


class _TreeLoader(yaml.SafeLoader):
    """``safe_load`` for a file someone else wrote: no aliases (a few lines of them expand to billions of nodes, and
    every walk over the tree below would follow each one) and a bounded depth (past it the parser overflows the
    stack)."""

    _depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.composer.ComposerError(None, None, "aliases are not read in a project config", None)
        self._depth += 1
        try:
            if self._depth > _MAX_DEPTH:
                raise yaml.composer.ComposerError(None, None, "nested too deeply", None)
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _read_project(path: Path, project: Path, text: str | None) -> Any:
    """A project's config file (or ``text``, its trusted copy) through ``_TreeLoader``: a regular file of bounded size
    that, if it is a symlink, stays inside ``project``. A missing one is ``{}``, as with ``confio.read_yaml``."""
    if text is None:
        if not path.is_file():
            return {}
        text = trust.read_bounded(path, within=project).decode("utf-8")
    data = yaml.load(text, Loader=_TreeLoader) or {}  # noqa: S506 - a SafeLoader subclass
    if not isinstance(data, dict):
        raise confio.ConfigError(f"{path}: top level must be a mapping")
    return data


def _read(path: Path, text: str | None = None, *, project: Path | None = None) -> Any:
    """The config file ``path`` as written, or ``text``, its trusted copy. One that exists but does not parse names no
    secrets: refuse rather than export its copies in clear. The error carries no parser text, not even chained (it is
    raised after the ``suppress``): a YAML error quotes the line, which may hold the secret, and a traceback or
    ``logger.exception`` prints the whole chain.

    ``project`` is the project directory when ``path`` is its config, which a cloned repo may have made hostile: it is
    read as ``_read_project`` does, and a file that hangs, overflows or points out of the project is refused too. Not
    when ``path`` is the user's own config (a session in ``~`` with ``K3CODE_HOME=~/.k3code`` sees it as the project's):
    that is theirs, aliases and a link to a dotfiles repo included."""
    if project is not None and trust.project_dir_of(path) is None:
        project = None
    with contextlib.suppress(OSError, ValueError, yaml.YAMLError, RecursionError):
        if project is not None:
            return _read_project(path, project, text)
        return confio.read_yaml(path) if text is None else yaml.safe_load(text)
    raise BundleError(_unparsed(path))


def _unparsed(path: Path) -> str:
    return f"{path} does not parse: fix it first, or the export cannot tell which of its values to hide"


def _known_secrets(*configs: Any) -> tuple[list[str], list[str]]:
    """Every secret value the ``configs`` and this process know (provider keys, secret-named env vars, MCP values),
    longest first; and the same without the guessed ones (``debugdump.secret_values(guesses=False)``)."""
    from k3code.debugdump import secret_values

    def known(guesses: bool) -> list[str]:
        return sorted(set().union(*(secret_values(c, guesses=guesses) for c in configs)), key=len, reverse=True)

    return known(True), known(False)


def _drop_rules(tool: str, rules: Any, secrets: list[str], certain: list[str]) -> Any:
    if not isinstance(rules, dict):  # `bash: ask`, or the hardline regexes
        return drop_values(rules, certain if tool == "hardline" else secrets)
    # only an exact `deny` or `ask` is a rule that hiding a guessed value would break (rules.Rule rejects any other)
    drop = {p: certain if a in ("deny", "ask") else secrets for p, a in rules.items()}
    return {drop_values(p, drop[p]): drop_values(a, secrets) for p, a in rules.items()}


def _drop_known(settings: dict[str, Any], secrets: list[str], certain: list[str]) -> dict[str, Any]:
    """``settings`` without the known secret values (``drop_values``). A deny or ask rule's pattern and a hardline
    regex lose only the ``certain`` ones: hiding a guessed value (an MCP root path, a region) there leaves a rule that
    never matches, and the import then allows what it denied. Every other rule (allow, or an action no rule accepts)
    and every key, tool names included, loses every known value: an allow rule then only asks again."""
    return {
        drop_values(key, secrets): {
            drop_values(tool, secrets): _drop_rules(tool, rules, secrets, certain) for tool, rules in value.items()
        }
        if key == "permissions" and isinstance(value, dict)
        else drop_values(value, secrets)
        for key, value in settings.items()
    }


def _secret_scalar_key(key: Any) -> bool:
    """``key`` names a secret (``is_secret_key``) for a number or bool under it. The word ``tokens`` alone names a
    count (`max_tokens: 4096`, `tokens_per_usd`), not a credential; ``token`` still names one."""
    return is_secret_key(_TOKENS_WORD.sub("", str(key)))


def _secret_scalars_as_text(obj: Any, secrets: list[str], section: bool = False) -> Any:
    """``obj`` with each number or bool that is a secret as its text, so ``drop_values`` and ``redact`` (strings
    only) hide it: one in an MCP ``env``/``headers`` section, one under a secret-named key, however short (a top-level
    or provider `api_key: 58217`), or one whose text holds a known secret (an unquoted `SERVICE_TOKEN: 8274619305`).
    Any other keeps its type (`max_tokens: 4096`)."""
    if isinstance(obj, dict):
        return {
            k: _secret_scalars_as_text(
                v, secrets, section or str(k).lower() in _SECRET_SECTIONS or _secret_scalar_key(k)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_secret_scalars_as_text(v, secrets, section) for v in obj]
    if isinstance(obj, int | float | bool) and (section or any(s in str(obj) for s in secrets)):
        return str(obj)
    return obj


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
    configs, written = _configs(config, cwd)
    secrets = _known_secrets(*configs)[0]
    files: dict[str, bytes] = {}
    if settings:
        for name, kind in _SETTINGS_FILES.items():
            src = user_config_path() if kind == "user" else project_config_path(cwd)
            if src.is_file():
                raw = _read(src, project=cwd if kind == "project" else None)
                # its own values hide their copies in it even when nothing else counts it (an untrusted project
                # config), but only in it: a file the user never trusted must not hide part of one of their rules
                secrets_here, certain = _known_secrets(*configs, raw)
                data = redact(_drop_known(_secret_scalars_as_text(raw, secrets_here), secrets_here, certain))
                files[name] = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()
    # the project config as written, trusted or not, hides its values in the sessions: the certain ones only, so a file
    # the user never trusted cannot blank out a region or path their transcripts name (a guess, see secret_values)
    secrets = sorted({*secrets, *_known_secrets(written)[1]}, key=len, reverse=True)
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
    """Drop ``<redacted>`` values from the dicts inside ``obj``. A list keeps the marker in place: without it every
    later argument of ``--model <redacted> --port 9000`` would shift (``--model --port 9000``)."""
    if isinstance(obj, dict):
        return {k: _strip_redacted(v) for k, v in obj.items() if v != REDACTED}
    if isinstance(obj, list):
        return [_strip_redacted(v) for v in obj]
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

    def describe(self) -> str:
        lines = []
        for f in self.settings_written:
            lines.append(f"settings written: {f}")
        for key in self.skipped:
            lines.append(
                f"warning: skipped {key} from the bundle (runs code, approves tools or sends keys; accept it item by "
                "item in an interactive import, or pass --trust-bundle)"
            )
        for b in self.backups:
            lines.append(f"backup: {b}")
        for old, new in self.sessions.items():
            lines.append(f"session {old}" + (f" → {new} (id collided)" if old != new else " imported"))
        if self.untrusted_project:
            lines.append(
                f"{self.untrusted_project} is not trusted yet: `k3code trust` shows what it changes and applies it"
            )
        return "\n".join(lines) or "nothing imported"


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
            sess = StoredSession(
                **{k: obj[k] for k in StoredSession.__dataclass_fields__ if k in obj and k != "session_id"},
                session_id=str(obj["session_id"]),
            )
            sess.meta = {
                k: v
                for k, v in (sess.meta if isinstance(sess.meta, dict) else {}).items()
                if k not in _UNTRUSTED_META and not k.startswith("origin")
            }
            old = sess.session_id
            if store.get(old) is not None:
                sess.session_id = store.new_id()
            store.insert(sess)
            rep.sessions[old] = sess.session_id
    return rep
