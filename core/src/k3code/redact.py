"""Secret redaction for exported config/sessions.

A value is replaced by ``"<redacted>"`` when its key matches ``key|token|secret|password``
(env-var *names* such as ``api_key_env`` are kept; numbers/bools are never secrets) or when
the value itself looks like a credential (known prefixes, JWTs, bearer headers, URL userinfo,
long opaque tokens).
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"

_KEY_RE = re.compile(r"key|token|secret|passw|pwd|credential|authoriz|cookie|(?<![a-z])auth(?![a-z])", re.I)
#: Config sections whose every value is a secret by default (MCP server environments and request headers).
_SECRET_SECTIONS = frozenset({"env", "environment", "headers"})
_PREFIXED = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{12,}|sk_(?:live|test)_[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|glpat-[A-Za-z0-9_\-]{16,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}|Bearer\s+[A-Za-z0-9._\-~+/]{16,}"
    r"|://[^/\s:@]+:[^/\s@]+@)"
)
_OPAQUE = re.compile(r"[A-Za-z0-9_\-]{32,}")


def is_secret_key(key: str) -> bool:
    k = str(key)
    if k.lower().endswith("_env") or k.lower() in ("api_key_env", "env_var"):
        return False  # names of env vars, not secrets
    return bool(_KEY_RE.search(k))


def looks_like_secret(value: str) -> bool:
    if value == REDACTED:
        return False
    if _PREFIXED.search(value):
        return True
    v = value.strip()
    return bool(_OPAQUE.fullmatch(v)) and any(c.isdigit() for c in v) and any(c.isalpha() for c in v)


_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
#: `DB_PASSWORD=hunter2`, `export API_KEY: "abc123"`, `client_secret = xyz` (the value stays hidden, the name stays).
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_.\-]*(?:password|passwd|secret|token|api[_-]?key|apikey|private[_-]?key|credential|"
    r"access[_-]?key|auth)[A-Z0-9_.\-]*)(\s*[=:]\s*)(['\"]?)([^\s'\"<>,;]{6,})\3"
)
_AUTH_HEADER = re.compile(
    r"(?i)\b((?:proxy-)?authorization|set-cookie|cookie|x-api-key|x-auth-token)(\s*[:=]\s*)"
    r"(?:(?:basic|bearer|token|digest)\s+)?[^\s'\"]{6,}"
)
_URL_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|client[_-]?secret|key|password|"
    r"passwd|pwd|sig|signature)=)[^&\s\"']+"
)
_SECRET_FLAG = re.compile(
    r"(?i)--?(?:password|passwd|token|api[_-]?key|apikey|secret|client[_-]?secret|auth(?:[_-]?token)?)"
)
_FLAG_SECRET = re.compile(
    r"(?i)(\s--?(?:password|passwd|token|api[_-]?key|apikey|secret|client[_-]?secret|auth(?:[_-]?token)?)(?:=|\s+))"
    r"[^\s'\"-][^\s'\"]*"
)


def scrub_text(text: str) -> str:
    """Free text (session messages, URLs, command lines): mask credential shapes.

    Well-known prefixed tokens, private key blocks, ``NAME_PASSWORD=value`` style assignments, ``Authorization`` /
    ``Cookie`` header values, URL query secrets (``?api_key=...``) and ``--password value`` style flags.
    """
    text = _PEM.sub(REDACTED, text)
    text = _PREFIXED.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(3)}", text)
    text = _URL_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return _FLAG_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", text)


def redact(obj: Any, *, _force: bool = False) -> Any:
    """Deep copy of ``obj`` with secrets replaced by ``"<redacted>"``."""
    if isinstance(obj, dict):
        return {
            k: redact(v, _force=_force or is_secret_key(k) or str(k).lower() in _SECRET_SECTIONS)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        out: list[Any] = []
        hide_next = False  # an argv-style list: the element after `--token` is the token
        for v in obj:
            if hide_next and isinstance(v, str) and not v.startswith("-"):
                out.append(REDACTED)
                hide_next = False
                continue
            hide_next = isinstance(v, str) and bool(_SECRET_FLAG.fullmatch(v))
            out.append(redact(v, _force=_force))
        return out
    if isinstance(obj, str):
        if obj == "":
            return obj
        # not a secret by name or shape: it can still carry one (a URL with ?api_key=, an arg list with --token x)
        return REDACTED if _force or looks_like_secret(obj) else scrub_text(obj)
    return obj


def redact_session_json(obj: Any) -> Any:
    """Session payloads: scrub credential-shaped substrings in every string."""
    if isinstance(obj, dict):
        return {k: redact_session_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_session_json(v) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj
