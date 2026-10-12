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
    r"|Bearer\s+(?=[A-Za-z0-9._\-~+/]*[0-9._\-~+/])[A-Za-z0-9._\-~+/]{6,}"
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
#: A flag that takes a credential: `--token`, `--github-token`, `--pat`, `--api-key`, `--client-secret` ...
_FLAG_NAME = (
    r"--?(?:[a-z0-9]+[_-])*(?:password|passwd|pwd|token|api[_-]?key|apikey|secret|client[_-]?secret|"
    r"auth(?:[_-]?(?:token|key))?|bearer|credentials?|access[_-]?key|private[_-]?key|pat|key)"
)
_SECRET_FLAG = re.compile(r"(?i)" + _FLAG_NAME)
_FLAG_SECRET = re.compile(r"(?i)((?:^|\s)" + _FLAG_NAME + r"(?:=|\s+))[^\s'\"-][^\s'\"]*")
_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"<>]+", re.I)
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_\-+=~]+")
_HEX = re.compile(r"[0-9a-fA-F]+")
_SLUG_WORD = re.compile(r"[a-z]+[0-9]{0,4}|[0-9]{1,4}")


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


def _token_shaped(part: str, min_len: int) -> bool:
    """``part`` is a run of base64/hex-like characters of at least ``min_len`` that is not an ordinary word or slug
    (``getting-started-guide``); a false positive hides a harmless value, a miss would leak a credential."""
    if len(part) < min_len or not _TOKEN_CHARS.fullmatch(part):
        return False
    if _HEX.fullmatch(part):
        return len(part) >= 20 or any(c.isalpha() for c in part)
    words = re.split(r"[-_]", part)
    if len(words) > 1 and all(_SLUG_WORD.fullmatch(w) for w in words):
        return False
    return any(c.isdigit() for c in part) or (part != part.lower() and part != part.upper())


def _scrub_url(url: str) -> str:
    """Hide what a URL can carry whatever its key is called: a password, a token-shaped user name, token-shaped path
    segments (20+ characters, e.g. ``/mcp/<token>/sse``) and token-shaped query values (8+)."""
    scheme, _, rest = url.partition("://")
    m = re.fullmatch(r"([^/?#]*)([^?#]*)(\?[^#]*)?(#.*)?", rest, re.S)
    if not m:
        return url
    authority, path, query, frag = m.group(1), m.group(2), m.group(3) or "", m.group(4) or ""
    user, at, host = authority.rpartition("@")
    if at:
        name, colon, _pw = user.partition(":")
        user = f"{name}:{REDACTED}" if colon else (REDACTED if _token_shaped(user, 20) else user)
        authority = f"{user}@{host}"
    path = "/".join(REDACTED if _token_shaped(seg, 20) else seg for seg in path.split("/"))
    if query:
        pairs = []
        for pair in query[1:].split("&"):
            key, eq, value = pair.partition("=")
            pairs.append(f"{key}={REDACTED}" if eq and _token_shaped(value, 8) else pair)
        query = "?" + "&".join(pairs)
    return f"{scheme}://{authority}{path}{query}{frag}"


def scrub_urls(text: str) -> str:
    """Every URL in ``text`` through ``_scrub_url`` (a trailing ``.,;)`` is not part of the URL)."""

    def one(m: re.Match[str]) -> str:
        url = m.group(0)
        core = url.rstrip(".,;:!?)]}")
        return _scrub_url(core) + url[len(core) :]

    return _URL.sub(one, text)


def drop_values(obj: Any, secrets: list[str]) -> Any:
    """``obj`` with every occurrence of a known secret value replaced, whatever its shape (scrub_text is by shape)."""
    if isinstance(obj, dict):
        return {k: drop_values(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [drop_values(v, secrets) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            if s in obj:
                obj = obj.replace(s, REDACTED)
    return obj


def redact(obj: Any, *, _force: bool = False, _argv: bool = False) -> Any:
    """Deep copy of ``obj`` with secrets replaced by ``"<redacted>"``."""
    if isinstance(obj, dict):
        return {
            k: redact(v, _force=_force or is_secret_key(k) or str(k).lower() in _SECRET_SECTIONS, _argv=k == "args")
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
            if _argv and isinstance(v, str) and _token_shaped(v, 20):
                out.append(REDACTED)  # a lone argument of token shape in an argv list (`args`)
                continue
            out.append(redact(v, _force=_force))
        return out
    if isinstance(obj, str):
        if obj == "":
            return obj
        # not a secret by name or shape: it can still carry one (a URL with ?api_key=, an arg list with --token x)
        return REDACTED if _force or looks_like_secret(obj) else scrub_urls(scrub_text(obj))
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
