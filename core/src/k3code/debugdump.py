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

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
_SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\",}]{6,}"),
)
REDACTED = "[REDACTED]"


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
    return sorted((v for v in values if len(v) >= 6), key=len, reverse=True)


def redact_text(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, REDACTED)
    for pat in _SECRET_SHAPES:
        text = pat.sub(
            lambda m: (m.group(1) + m.group(2) + REDACTED) if m.lastindex and m.lastindex >= 2 else REDACTED, text
        )
    return text


def drop_values(obj: Any, secrets: list[str]) -> Any:
    """``obj`` with every occurrence of a known secret value replaced, whatever its shape."""
    if isinstance(obj, dict):
        return {k: drop_values(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [drop_values(v, secrets) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, REDACTED)
    return obj


def safe_config(config: Any, secrets: list[str] | None = None) -> dict[str, Any]:
    """The config as a plain mapping with secrets out. By structure (a key named like a secret, every value under
    ``env`` and ``headers``, a URL with a password in it), then by value (the keys this process knows). The text
    patterns alone left an MCP server's ``postgres://user:pw@host/db`` env value and ``X-Api-Auth`` header in clear."""
    from k3code.redact import redact

    cfg = redact(config.model_dump(mode="json"))
    for p in cfg.get("providers", []):
        p["api_key"] = REDACTED if p.get("api_key") else ""
    return drop_values(cfg, secrets if secrets is not None else secret_values(config))


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
    events = list(server.event_log)[-n:] if n > 0 else []  # [-0:] is the whole list
    checks = await doctor.run_checks(server.config, probe=probe, home=home)
    parts = {
        "events.json": json.dumps(events, indent=2, default=str),
        "config.json": json.dumps(safe_config(server.config, secrets), indent=2, default=str),
        "versions.json": json.dumps(versions(), indent=2),
        "doctor.json": doctor.to_json(checks),
    }
    out_dir = home / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    # microseconds in the name: two dumps in one second overwrote each other
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1e6) % 1_000_000:06d}"
    path = out_dir / f"{stamp}.tar.gz"
    n_dup = 0
    while path.exists():
        n_dup += 1
        path = out_dir / f"{stamp}-{n_dup}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, body in parts.items():
            data = redact_text(body, secrets).encode("utf-8")
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
