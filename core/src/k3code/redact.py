"""Secret redaction for exported config/sessions.

A value is replaced by ``"<redacted>"`` when its key matches ``key|token|secret|password``
(env-var *names* such as ``api_key_env`` are kept; numbers/bools are never secrets) or when
the value itself looks like a credential (known prefixes, JWTs, bearer headers, URL userinfo,
long opaque tokens). Dict keys are scrubbed too (a permission rule such as ``npx tool --token sk-...`` is a key).
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"

_KEY_RE = re.compile(r"key|token|secret|passw|pwd|credential|authoriz|cookie|(?<![a-z])auth(?![a-z])", re.I)
#: Config sections whose every value is a secret by default (MCP server environments and request headers).
_SECRET_SECTIONS = frozenset({"env", "environment", "headers"})
_PREFIXED = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{12,}|[spr]k_(?:live|test)_[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|glpat-[A-Za-z0-9_\-]{16,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}|Bearer\s+[A-Za-z0-9._\-~+/]{16,}"
    r"|Bearer\s+(?=[A-Za-z0-9._\-~+/]*[0-9._\-~+/])[A-Za-z0-9._\-~+/]{6,}"
    r"|://[^/\s:@]+:[^/\s@]+@"
    # Hugging Face, npm, AWS temporary key ids (ASIA...), SendGrid
    r"|\bhf_[A-Za-z0-9]{30,}|\bnpm_[A-Za-z0-9]{30,}|\bASIA[0-9A-Z]{16}\b|\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"
    # webhook URLs: the path is the credential
    r"|https?://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_\-]{8,}"
    r"|https?://(?:[a-z]+\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_\-]{8,})"
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


_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S
)
#: A value: quoted (it may hold spaces, and the other kind of quote) or a run of 6+ characters with no delimiter.
_VALUE = r"""(?:(?P<q>['"])(?P<qv>(?:\\.|(?!(?P=q))[^\r\n\\]){6,}?)(?P=q)|(?P<v>[^\s'"<>,;]{6,}))"""
_NAME_KEYWORDS = r"password|passwd|secret|token|api[_-]?key|apikey|private[_-]?key|credential|access[_-]?key|auth"
#: `DB_PASSWORD=hunter2`, `export API_KEY: "abc123"`, `"client_secret": "xyz"` (the value stays hidden, the name stays).
#: The match starts at the keyword, not at the start of the name, so a long run of name characters is scanned once.
_ASSIGNMENT = re.compile(rf"(?i)(?P<name>(?:{_NAME_KEYWORDS})[A-Z0-9_.\-]*+)(?P<kq>['\"]?)(?P<sep>\s*[=:]\s*)" + _VALUE)
#: `DB_PASS=...`, `Pwd=...;` and `"pwd": "..."`: a whole `pass`/`pwd` word in the name (not `bypass`, not the command
#: `pwd`), after `=` (or a colon behind a quoted key), so that prose such as `pass: successfully` is left alone.
_PASS_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Z0-9])(?P<name>(?:pass|pwd)(?:[_.\-][A-Z0-9]+){0,6}+)"
    r"(?P<kq>['\"]?)(?P<sep>(?<=['\"])[ \t]*[=:][ \t]*|(?<!['\"])[ \t]*=[ \t]*)" + _VALUE
)
#: `git log --author="Jane Doe"` is not a credential: `auth` is a keyword, `author` is not.
_AUTHOR = re.compile(r"(?i)author(?!iz)")
_OTHER_KEYWORD = re.compile(_NAME_KEYWORDS, re.I)
_NUMBER = re.compile(r"[0-9][0-9_.,eE+\-]*")
_AUTH_HEADER = re.compile(
    r"(?i)\b((?:proxy-)?authorization|x-api-key|x-auth-token)(['\"]?\s*[:=]\s*['\"]?)"
    r"(?:(?:basic|bearer|token|digest)\s+)?[^\s'\"]{6,}"
)
#: A cookie header holds several `name=value` pairs; all of them go (a closing quote ends the value).
_COOKIE_HEADER = re.compile(
    r"(?i)\b(set-cookie|cookie)(['\"]?\s*[:=]\s*['\"]?)[^\s'\"]{6,}(?:\s+[^\s'\"=;]+=[^\s'\";]*;?)*"
)
_URL_SECRET = re.compile(
    r"(?i)([?&#](?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"session[_-]?token|token|secret|client[_-]?secret|key|password|passwd|pwd|sig|signature|x-amz-signature|"
    r"x-amz-security-token|x-goog-signature)=)[^&\s\"']+"
)
#: An OAuth `?code=`: 10+ characters, so `?code=200` and `?code=US` stay readable.
_URL_CODE = re.compile(r"(?i)([?&#](?:code|auth(?:orization)?[_-]?code)=)[^&\s\"']{10,}")
#: A command line that carries a password as an argument: `mysql -pSECRET`, `sshpass -p SECRET`, `curl -u user:SECRET`.
_ARG = r"""(?:'[^'\n]*'|"[^"\n]*"|[^\s'"]+)"""
_MYSQL_P = re.compile(r"(?i)(\bmysql\w*\b[^\n]{0,1000}?\s-(?-i:p))(?=\S)" + _ARG)
_SSHPASS = re.compile(r"(\bsshpass\b[^\n]{0,1000}?\s-p\s*)" + _ARG)
_CURL_USER = re.compile(
    r"(\bcurl\b[^\n]{0,1000}?\s(?:-[A-Za-z]*[uU]\s*|--(?:proxy-)?user[=\s]\s*))"
    # the part before the first colon cannot hold one: an unclosed quote full of colons is scanned once, not per colon
    r"""(?:'[^'\n:]*:[^'\n]*'|"[^"\n:]*:[^"\n]*"|[^\s'":]*:[^\s'"]*)"""
)
_SECRET_FLAG = re.compile(
    r"(?i)--?(?:password|passwd|token|api[_-]?key|apikey|secret|client[_-]?secret|auth(?:[_-]?token)?)"
)
_FLAG_SECRET = re.compile(
    r"(?i)((?:^|\s)--?(?:password|passwd|token|api[_-]?key|apikey|secret|client[_-]?secret|auth(?:[_-]?token)?)(?:=|\s+))"
    r"[^\s'\"-][^\s'\"]*"
)


def _mask_assignment(m: re.Match[str]) -> str:
    value = m["v"]
    if value is not None and (m["kq"] and _NUMBER.fullmatch(value)):
        return m.group(0)  # `"max_tokens": 100000`: a JSON number is not a credential
    if value is not None and m["name"] == "PWD" and value.startswith("/"):
        return m.group(0)  # the shell's `PWD=/home/user/project`
    if m["q"] and _AUTHOR.search(m["name"]) and not _OTHER_KEYWORD.search(_AUTHOR.sub("", m["name"])):
        return m.group(0)  # `--author="Jane Doe"`
    masked = f"{m['q']}{REDACTED}{m['q']}" if m["q"] else REDACTED
    return f"{m['name']}{m['kq']}{m['sep']}{masked}"


def scrub_text(text: str) -> str:
    """Free text (session messages, URLs, command lines): mask credential shapes.

    Well-known prefixed tokens (including webhook URLs), private key blocks, ``NAME_PASSWORD=value`` / JSON
    ``"api_key": "value"`` style assignments, ``Authorization`` / ``Cookie`` header values, URL query secrets
    (``?api_key=...``, ``?code=...``, ``X-Amz-Signature=...``), ``--password value`` style flags and the passwords on
    ``mysql -p``, ``sshpass -p`` and ``curl -u`` command lines.
    """
    text = _PEM.sub(REDACTED, text)
    text = _PREFIXED.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _COOKIE_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _ASSIGNMENT.sub(_mask_assignment, text)
    text = _PASS_ASSIGNMENT.sub(_mask_assignment, text)
    text = _URL_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _URL_CODE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _MYSQL_P.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _SSHPASS.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _CURL_USER.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return _FLAG_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", text)


def _scrub_key(key: Any) -> Any:
    """A dict key can carry a credential too (a permission rule ``npx tool --token sk-...``): scrub it by shape."""
    return scrub_text(key) if isinstance(key, str) else key


def redact(obj: Any, *, _force: bool = False) -> Any:
    """Deep copy of ``obj`` with secrets replaced by ``"<redacted>"``."""
    if isinstance(obj, dict):
        return {
            _scrub_key(k): redact(v, _force=_force or is_secret_key(k) or str(k).lower() in _SECRET_SECTIONS)
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
        return {_scrub_key(k): redact_session_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_session_json(v) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj
