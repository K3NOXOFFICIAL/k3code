"""``/debug dump``: a redacted support bundle (events, config without secrets, versions, doctor, prompt hashes)."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import sys
import tarfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from k3code.daemon import k3_home
from k3code.redact import (
    REDACTED,
    _token_shaped,
    drop_values,
    is_secret_key,
    looks_like_secret,
    redact,
    scrub_text,
    scrub_urls,
)

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
_SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\",}]{6,}"),
)
#: `Authorization: Bearer <token>`: the token alone is a secret too.
_AUTH_SCHEME = re.compile(r"(?i)(?:bearer|basic|token|digest|negotiate|api-?key|sso-key)\s+(\S{6,})")
#: Words and short numbers between `/ . :` (`127.0.0.1`, `https://api.github.com`, `Europe/Berlin`, `en_US.UTF-8`).
_ORDINARY_PART = re.compile(r"[A-Z]?[a-z]+[0-9]{0,4}|[A-Z]+|[0-9]{1,5}")


def _secret_shape(value: str) -> bool:
    """``value`` is or carries a credential the shape scrubber knows (a prefixed token, a URL password, ``?token=``)."""
    return looks_like_secret(value) or scrub_text(scrub_urls(value)) != value


def _ordinary(value: str) -> bool:
    """An MCP env/header value that is a word, path, address, locale or MIME type rather than a credential: hiding it
    wherever it appears would break every setting that repeats it (`http://127.0.0.1:4000/v1`)."""
    if _secret_shape(value):
        return False
    if value.isalpha():
        return True
    if value.startswith(("/", "~")):  # a path, unless it is only tokens (standard base64 can start with `/`): one
        # ordinary word makes it a path whatever its directories are called (`/Users/dev/Code/MyAwesomeProject`)
        segs = [seg for seg in re.split(r"[/~]", value) if seg]
        return any(_ORDINARY_PART.fullmatch(seg) for seg in segs) or not any(_token_shaped(seg, 16) for seg in segs)
    if not re.search(r"[/.:]", value):
        return False
    return all(_ORDINARY_PART.fullmatch(p) for p in re.split(r"[/.:@?&=#_\-]+", value) if p)


def _env_refs(obj: Any) -> set[str]:
    """Names of the variables that ``*_env`` settings point at (``api_key_env``, ``bearer_env``): credentials."""
    if isinstance(obj, dict):
        names = {v for k, v in obj.items() if str(k).endswith("_env") and isinstance(v, str) and v}
        return names.union(*(_env_refs(v) for v in obj.values()))
    if isinstance(obj, list):
        return set().union(*(_env_refs(v) for v in obj))
    return set()


def secret_values(config: Any = None, *, guesses: bool = True) -> list[str]:
    """Every secret value this process knows, longest first: env vars that look secret, provider keys, the variables
    ``*_env`` settings name (``bearer_env``), and MCP env/header values that are secret by name or by shape. Each also
    in its JSON-escaped form, for JSON text inside a tool result, and percent-encoded, for a URL.

    ``guesses=False`` leaves out the MCP values known only because they are no ordinary word, path or address (a
    region, a database name): a deny rule that names one must keep it (see ``bundle.write_bundle``)."""
    from k3code.config import env_value

    values: set[str] = set()
    for name, value in os.environ.items():
        if value and len(value) >= 6 and _SECRET_NAME.search(name):
            values.add(value)
    for p in getattr(config, "providers", None) or []:
        if getattr(p, "api_key", ""):
            values.add(p.api_key)
    dump = config.model_dump(mode="json") if hasattr(config, "model_dump") else {}
    values.update(env_value(name) for name in _env_refs(dump))
    # MCP env/headers are hidden in place whatever they hold; out of place only a value secret by name or not an
    # ordinary word, path or address (an MCP `HOST=127.0.0.1` must not hide a provider's `http://127.0.0.1:4000/v1`)
    for server in getattr(getattr(config, "mcp", None), "servers", None) or {}:
        cfg = config.mcp.servers[server]
        for name, value in [*(getattr(cfg, "env", None) or {}).items(), *(getattr(cfg, "headers", None) or {}).items()]:
            if not isinstance(value, str):
                continue
            known = _SECRET_NAME.search(name) or is_secret_key(name) or _secret_shape(value)
            if known or (guesses and len(value) >= 8 and not _ordinary(value)):
                values.add(value)
            if m := _AUTH_SCHEME.fullmatch(value.strip()):
                values.add(m.group(1))
    out = {
        f
        for v in values
        if len(v) >= 6
        for f in (v, json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1], quote(v, safe=""))
    }
    return sorted(out, key=len, reverse=True)


def redact_text(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, REDACTED)
    for pat in _SECRET_SHAPES:
        text = pat.sub(
            lambda m: (m.group(1) + m.group(2) + REDACTED) if m.lastindex and m.lastindex >= 2 else REDACTED, text
        )
    return text


def safe_config(config: Any, secrets: list[str] | None = None) -> dict[str, Any]:
    """The config without credentials: every known secret value wherever it appears, then k3code.redact by
    structure (key names, env/headers, URL parts, secret-flag argv)."""
    return redact(drop_values(config.model_dump(mode="json"), secrets or []))


def scrub_json(body: str, secrets: list[str]) -> str:
    """A JSON document through the same two passes, known values first: a shape pass can cut a secret at a `,` or a
    quote and leave its tail for the value pass to miss (text that is not JSON gets ``redact_text`` only)."""
    try:
        data = json.loads(body)
    except ValueError:
        return redact_text(body, secrets)
    return json.dumps(redact(drop_values(data, secrets)), indent=2, default=str)


def versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version

    try:
        k3 = version("k3code")
    except PackageNotFoundError:
        k3 = "unknown"
    from k3code.paths import resolve_node

    node = resolve_node()
    node_v = node.version or node.old.split(" ")[0]
    return {"k3code": k3, "python": sys.version.split()[0], "platform": platform.platform(), "node": node_v}


async def write_bundle(server: Any, n: int = 200, home: Path | None = None, *, probe: bool = True) -> Path:
    """Write ``$K3CODE_HOME/debug/<ts>.tar.gz`` and return its path."""
    from k3code import doctor
    from k3code.config import default_project_dir
    from k3code.prompting import prompt_fingerprints

    home = home or k3_home()
    secrets = secret_values(server.config)
    events = list(server.event_log)[-n:]
    checks = await doctor.run_checks(server.config, probe=probe, home=home)
    parts = {
        "events.json": json.dumps(events, indent=2, default=str),
        "config.json": json.dumps(safe_config(server.config, secrets), indent=2, default=str),
        "versions.json": json.dumps(versions(), indent=2),
        "doctor.json": doctor.to_json(checks),
        # hashes and section sizes only: the text carries project memory, so it stays out of a bundle that is shared
        "prompt.json": json.dumps(prompt_fingerprints(server.config, default_project_dir()), indent=2),
    }
    out_dir = home / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, body in parts.items():
            data = redact_text(scrub_json(body, secrets), secrets).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    path.chmod(0o600)
    from k3code.config import retention

    prune_bundles(out_dir, keep=retention(server.config)["debug_bundles"])
    return path


def prune_bundles(out_dir: Path, keep: int) -> int:
    """Keep the newest ``keep`` bundles in ``out_dir`` (names are timestamps); returns how many were removed."""
    bundles = sorted(p for p in out_dir.glob("*.tar.gz") if p.is_file())
    removed = 0
    for old in bundles[: max(0, len(bundles) - keep)]:
        old.unlink(missing_ok=True)
        removed += 1
    return removed
