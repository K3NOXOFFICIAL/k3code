"""``/debug dump``: a redacted support bundle (recent events, config without secrets, versions, doctor)."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

from k3code.daemon import k3_home
from k3code.redact import REDACTED, drop_values, redact

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
_SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\",}]{6,}"),
)


def secret_values(config: Any = None) -> list[str]:
    """Every secret value this process knows (env vars that look secret, provider keys), longest first."""
    values: set[str] = set()
    for name, value in os.environ.items():
        if value and len(value) >= 6 and _SECRET_NAME.search(name):
            values.add(value)
    for p in getattr(config, "providers", None) or []:
        if getattr(p, "api_key", ""):
            values.add(p.api_key)
        env_val = os.environ.get(getattr(p, "api_key_env", ""), "")
        if env_val:
            values.add(env_val)
    # every value under an MCP server's env/headers is a secret by the redaction policy, whatever its name; plain words
    # (production), paths and short flags are not worth hiding wherever they appear
    for server in getattr(getattr(config, "mcp", None), "servers", None) or {}:
        cfg = config.mcp.servers[server]
        for value in [*(getattr(cfg, "env", None) or {}).values(), *(getattr(cfg, "headers", None) or {}).values()]:
            if isinstance(value, str) and len(value) >= 8 and not value.isalpha() and not value.startswith(("/", "~")):
                values.add(value)
    return sorted((v for v in values if len(v) >= 6), key=len, reverse=True)


def redact_text(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, REDACTED)
    for pat in _SECRET_SHAPES:
        text = pat.sub(
            lambda m: (m.group(1) + m.group(2) + REDACTED) if m.lastindex and m.lastindex >= 2 else REDACTED, text
        )
    return text


def safe_config(config: Any, secrets: list[str] | None = None) -> dict[str, Any]:
    """The config without credentials: k3code.redact by structure (key names, env/headers, URL parts, secret-flag
    argv), then every known secret value wherever it still appears."""
    return drop_values(redact(config.model_dump(mode="json")), secrets or [])


def scrub_json(body: str, secrets: list[str]) -> str:
    """A JSON document through the same two passes (text that is not JSON gets the shape regexes only)."""
    try:
        data = json.loads(body)
    except ValueError:
        return redact_text(body, secrets)
    return json.dumps(drop_values(redact(data), secrets), indent=2, default=str)


def versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version

    try:
        k3 = version("k3code")
    except PackageNotFoundError:
        k3 = "unknown"
    node = shutil.which("node")
    node_v = (
        subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout.strip() if node else ""
    )
    return {"k3code": k3, "python": sys.version.split()[0], "platform": platform.platform(), "node": node_v}


async def write_bundle(server: Any, n: int = 200, home: Path | None = None, *, probe: bool = True) -> Path:
    """Write ``$K3CODE_HOME/debug/<ts>.tar.gz`` and return its path."""
    from k3code import doctor

    home = home or k3_home()
    secrets = secret_values(server.config)
    events = list(server.event_log)[-n:]
    checks = await doctor.run_checks(server.config, probe=probe, home=home)
    parts = {
        "events.json": json.dumps(events, indent=2, default=str),
        "config.json": json.dumps(safe_config(server.config, secrets), indent=2, default=str),
        "versions.json": json.dumps(versions(), indent=2),
        "doctor.json": doctor.to_json(checks),
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
