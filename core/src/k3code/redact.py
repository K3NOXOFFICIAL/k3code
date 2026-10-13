"""Secret redaction for exported config/sessions.

A value is replaced by ``"<redacted>"`` when its key matches ``key|token|secret|password``
(env-var *names* such as ``api_key_env`` are kept; numbers/bools are never secrets) or when
the value itself looks like a credential (known prefixes, JWTs, bearer headers, URL userinfo,
long opaque tokens). Dict keys are scrubbed too, by shape only (a permission rule such as ``npx tool --token sk-...``
is a key; a long model id is a name).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

REDACTED = "<redacted>"

_KEY_RE = re.compile(r"key|token|secret|passw|pwd|credential|authoriz|cookie|(?<![a-z])auth(?![a-z])", re.I)
#: Config sections whose every value is a secret by default (MCP server environments and request headers).
_SECRET_SECTIONS = frozenset({"env", "environment", "headers"})
_PREFIXED = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{12,}|[spr]k_(?:live|test)_[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|glpat-[A-Za-z0-9_\-]{16,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}|Bearer\s+[A-Za-z0-9._\-~+/]{16,}"
    r"|Bearer\s+(?=[A-Za-z0-9._\-~+/]*[0-9._\-~+/])[A-Za-z0-9._\-~+/]{6,}"
    r"|://[^/\s:@]+:(?![*?]+@)[^/\s@]+@"
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
    r"x-amz-security-token|x-goog-signature)=)([^&\s\"']+)"
)
#: An OAuth `?code=`: 10+ characters, so `?code=200` and `?code=US` stay readable.
_URL_CODE = re.compile(r"(?i)([?&#](?:code|auth(?:orization)?[_-]?code)=)[^&\s\"']{10,}")
#: A command line that carries a password as an argument: `mysql -pSECRET`, `sshpass -p SECRET`, `curl -u user:SECRET`.
_ARG = r"""(?:'[^'\n]*'|"[^"\n]*"|[^\s'"]+)"""
_MYSQL_P = re.compile(r"(?i)(\bmysql\w*\b[^\n]{0,1000}?\s-(?-i:p))(?=\S)(" + _ARG + ")")
_SSHPASS = re.compile(r"(\bsshpass\b[^\n]{0,1000}?\s-p\s*)(" + _ARG + ")")
_CURL_USER = re.compile(
    r"(\bcurl\b[^\n]{0,1000}?\s(?:-[A-Za-z]*[uU]\s*|--(?:proxy-)?user[=\s]\s*))"
    # the part before the first colon cannot hold one: an unclosed quote full of colons is scanned once, not per colon
    r"""('[^'\n:]*:[^'\n]*'|"[^"\n:]*:[^"\n]*"|[^\s'":]*:[^\s'"]*)"""
)
#: A flag that takes a credential: `--token`, `--github-token`, `--pat`, `--api-key`, `--client-secret` ... A `--no-`
#: flag takes none, and `key` names one only after a credential word (`--sort-key`, `--recv-key` are not secrets).
_FLAG_NAME = (
    r"--?(?!no[_-])(?:[a-z0-9]+[_-])*(?:password|passwd|pwd|token|apikey|(?:client[_-]?)?secret|bearer|"
    r"credentials?|pat|auth(?:[_-]?token)?|"
    r"(?:api|access|private|secret|client|auth|license|subscription|account|master|signing)[_-]?key)"
)
_SECRET_FLAG = re.compile(r"(?i)" + _FLAG_NAME)
_FLAG_SECRET = re.compile(r"(?i)((?:^|\s)" + _FLAG_NAME + r"(?:=|\s+))([^\s'\"-][^\s'\"]*)")
#: A bare `--key` hides its value only when that is token-shaped (`sort --key=2,2`, `--key docs/a.txt` stay).
_KEY_FLAG = re.compile(r"(?i)--?key")
_KEY_FLAG_VALUE = re.compile(r"(?i)((?:^|\s)--?key(?:=|\s+))([^\s'\"-][^\s'\"]*)")
_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"<>]+", re.I)
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_\-+=~]+")
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_HEX = re.compile(r"[0-9a-fA-F]+")
_SLUG_WORD = re.compile(r"[a-z]+[0-9]{0,4}|[0-9]{1,8}")
#: A value made only of glob characters is a permission pattern (`* --pat *`, `curl *?sig=*`), not a credential:
#: hiding it turns an exported deny rule into one that never matches.
_GLOB = re.compile(r"[*?]+")


def _hide(value: str) -> str:
    return value if _GLOB.fullmatch(value) else REDACTED


def _hide_arg(prefix: str, value: str) -> str:
    """``prefix`` + a command-line password argument, hidden unless it is only glob characters (a deny rule)."""
    return prefix + (value if _GLOB.fullmatch(value.strip("'\"")) else REDACTED)


def _hide_curl_user(m: re.Match[str]) -> str:
    """``curl -u user:password``: all of ``user:password`` goes, unless the password is only glob characters."""
    return m[1] + (m[2] if _GLOB.fullmatch(m[2].strip("'\"").partition(":")[2]) else REDACTED)


def _mask_assignment(m: re.Match[str]) -> str:
    value = m["v"]
    if _GLOB.fullmatch(value if value is not None else m["qv"]):
        return m.group(0)  # `--token=*` in a deny rule is a pattern, not a credential
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
    text = _URL_SECRET.sub(lambda m: f"{m.group(1)}{_hide(m.group(2))}", text)
    text = _URL_CODE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _MYSQL_P.sub(lambda m: _hide_arg(m.group(1), m.group(2)), text)
    text = _SSHPASS.sub(lambda m: _hide_arg(m.group(1), m.group(2)), text)
    text = _CURL_USER.sub(_hide_curl_user, text)
    text = _FLAG_SECRET.sub(lambda m: f"{m.group(1)}{_hide(m.group(2))}", text)
    return _KEY_FLAG_VALUE.sub(lambda m: m.group(1) + (REDACTED if _token_shaped(m.group(2), 8) else m.group(2)), text)


def _token_shaped(part: str, min_len: int) -> bool:
    """``part`` is a run of base64/hex-like characters of at least ``min_len`` that is not an ordinary word or slug
    (``getting-started-guide``); a false positive hides a harmless value, a miss would leak a credential."""
    part = unquote(part)  # a percent-encoded token is still a token
    if "/" in part:  # standard base64; a path, `Europe/Berlin` or `@scope/pkg` is not one
        mixed = part != part.lower() and part != part.upper() and any(c.isdigit() for c in part)
        return len(part) >= min_len and bool(_BASE64.fullmatch(part)) and not part.startswith("/") and mixed
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
        name, colon, pw = user.partition(":")
        user = f"{name}:{_hide(pw)}" if colon else (REDACTED if _token_shaped(user, 20) else user)
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
        return {drop_values(k, secrets): drop_values(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [drop_values(v, secrets) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            if s in obj:
                obj = obj.replace(s, REDACTED)
    return obj


def _flag_value(flag: str, value: str) -> bool:
    """``value`` follows ``flag`` in an argv list and is its credential (after a bare ``--key``: if token-shaped)."""
    return bool(_SECRET_FLAG.fullmatch(flag)) or (bool(_KEY_FLAG.fullmatch(flag)) and _token_shaped(value, 8))


def _redact_key(key: Any) -> Any:
    """A dict key can carry a secret too (`permissions.bash` stores an approved command as the key): scrub it by shape,
    never by ``looks_like_secret``. A long model id (`claude-sonnet-4-5-20250929-thinking-extended-1m`) is opaque to
    that check but a name: two of them as ``models`` keys would collapse into one `<redacted>` key."""
    return _scrub_key(scrub_urls(key)) if isinstance(key, str) else key


def redact(obj: Any, *, keep: frozenset[str] = frozenset(), _force: bool = False, _argv: bool = False) -> Any:
    """Deep copy of ``obj`` with secrets replaced by ``"<redacted>"``.

    ``keep`` names fields that are not secret by their name (``key`` of a notification event); their values are
    still scrubbed by shape, and a secret parent (``headers``) still hides them.
    """
    if isinstance(obj, dict):
        return {
            _redact_key(k): redact(
                v,
                keep=keep,
                _force=_force or (is_secret_key(k) and k not in keep) or str(k).lower() in _SECRET_SECTIONS,
                _argv=k == "args",
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        out: list[Any] = []
        flag: Any = None  # an argv-style list: the element after `--token` is the token
        for v in obj:
            if isinstance(flag, str) and isinstance(v, str) and not v.startswith("-") and _flag_value(flag, v):
                out.append(REDACTED)
                flag = None
                continue
            flag = v
            if _argv and isinstance(v, str):
                # a lone argument of token shape in an argv list (`args`); of `--name=value` only the value
                name, eq, value = v.partition("=") if v.startswith("-") and "=" in v else ("", "", v)
                if _token_shaped(value, 20):
                    out.append(f"{name}{eq}{REDACTED}")
                    continue
            out.append(redact(v, keep=keep, _force=_force))
        return out
    if isinstance(obj, str):
        if obj == "":
            return obj
        # not a secret by name or shape: it can still carry one (a URL with ?api_key=, an arg list with --token x)
        return REDACTED if _force or looks_like_secret(obj) else scrub_text(scrub_urls(obj))
    return obj


def _scrub_key(key: Any) -> Any:
    """A dict key can carry a credential too (a permission rule ``npx tool --token sk-...``): scrub it by shape."""
    return scrub_text(key) if isinstance(key, str) else key


def redact_session_json(obj: Any) -> Any:
    """Session payloads: scrub credential-shaped substrings in every string."""
    if isinstance(obj, dict):
        return {_scrub_key(k): redact_session_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_session_json(v) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj
