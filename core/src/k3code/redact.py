"""Secret redaction for exported config/sessions.

A value is replaced by ``"<redacted>"`` when its key names a secret (``api_key``, ``token``, ``secret``, ``password``;
env-var *names* such as ``api_key_env`` are kept; numbers/bools are never secrets) or when
the value itself looks like a credential (known prefixes, JWTs, bearer headers, URL userinfo,
long opaque tokens).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote

REDACTED = "<redacted>"

_KEY_RE = re.compile(r"token|secret|passw|pwd|credential|authoriz|cookie|(?<![a-z])auth(?![a-z])", re.I)
#: ``key`` names a secret as a word or the end of one (``api_key``, ``apiKey``, ``ssh_keys``), not inside another word
#: (``keymap``, ``keyboard``). Case-sensitive: a capital after ``Key`` starts the next word.
_KEY_WORD_RE = re.compile(r"(?:key|Key)s?(?![a-z])|KEYS?(?![A-Z])")
#: Config sections whose every value is a secret by default (MCP server environments and request headers).
_SECRET_SECTIONS = frozenset({"env", "environment", "headers"})
#: Known token prefixes, bearer headers and URL user info. JWTs are found by _jwts: as one more alternative here, a
#: JWT was tried again at every `eyJ` of a long run and scanned to the end of the run each time (quadratic, K3NOX-356).
_PREFIXED = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{12,}|sk_(?:live|test)_[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|glpat-[A-Za-z0-9_\-]{16,}"
    r"|Bearer\s+[A-Za-z0-9._\-~+/]{16,}"
    r"|Bearer\s+(?=[A-Za-z0-9._\-~+/]*[0-9._\-~+/])[A-Za-z0-9._\-~+/]{6,}"
    r"|://[^/\s:@]+:(?![*?]+@)[^/\s@]+@)"
)
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-]+")
_OPAQUE = re.compile(r"[A-Za-z0-9_\-]{32,}")


def is_secret_key(key: str) -> bool:
    k = str(key)
    if k.lower().endswith("_env") or k.lower() in ("api_key_env", "env_var"):
        return False  # names of env vars, not secrets
    return bool(_KEY_RE.search(k) or _KEY_WORD_RE.search(k))


def _jwts(text: str) -> Iterator[tuple[int, int]]:
    """Each place, in order, where ``eyJ<t>{8,}\\.<t>{8,}\\.<t>{4,}`` (``<t>`` a token character) matches, with the end
    of that match: three runs of token characters joined by single dots, the first with an ``eyJ`` 8 or more characters
    before its end. Each run is looked at once."""
    first = second = None
    for third in _TOKEN_RUN.finditer(text):
        if (
            first is not None
            and second is not None
            and first.end() + 1 == second.start()
            and second.end() + 1 == third.start()
            and text[first.end()] == text[second.end()] == "."
            and second.end() - second.start() >= 8
            and third.end() - third.start() >= 4
            and first.end() - first.start() >= 11
        ):
            i = text.find("eyJ", first.start(), first.end() - 8)
            while i != -1:
                yield i, third.end()
                i = text.find("eyJ", i + 3, first.end() - 8)
        first, second = second, third


def _prefixed_spans(text: str) -> list[tuple[int, int]]:
    """Where _PREFIXED and _jwts match, left to right and without overlap, as one regex of both did: the match that
    starts first wins (no prefixed rule starts with ``eyJ``, so two never start at the same place)."""
    spans: list[tuple[int, int]] = []
    jwts = _jwts(text) if "eyJ" in text else iter(())
    jwt, m = next(jwts, None), _PREFIXED.search(text)
    while jwt is not None or m is not None:
        if m is None or (jwt is not None and jwt[0] < m.start()):
            span = jwt
        else:
            span = m.span()
        spans.append(span)
        while jwt is not None and jwt[0] < span[1]:
            jwt = next(jwts, None)
        if m is not None and m.start() < span[1]:
            m = _PREFIXED.search(text, span[1])
    return spans


def _hide_prefixed(text: str) -> str:
    out, last = [], 0
    for start, end in _prefixed_spans(text):
        out += (text[last:start], REDACTED)
        last = end
    return "".join(out) + text[last:]


def looks_like_secret(value: str) -> bool:
    if value == REDACTED:
        return False
    if _PREFIXED.search(value) or ("eyJ" in value and next(_jwts(value), None)):
        return True
    v = value.strip()
    return bool(_OPAQUE.fullmatch(v)) and any(c.isdigit() for c in v) and any(c.isalpha() for c in v)


_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
#: `DB_PASSWORD=hunter2`, `export API_KEY: "abc123"`, `client_secret = xyz` (the value stays hidden, the name stays).
#: Tried once per run of name characters, from its first word start (`\b`, as before), with an atomic name: starting
#: again at every word start inside a long run (`a-a-a-...`) or at every keyword in it (`tokentoken...`) was quadratic
#: (K3NOX-356).
_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Z0-9_.\-])((?>[A-Z0-9_.\-]*?\b)(?>[A-Z0-9_.\-]*?(?:password|passwd|secret|token|api[_-]?key|apikey|"
    r"private[_-]?key|credential|access[_-]?key|auth)[A-Z0-9_.\-]*))(\s*[=:]\s*)(['\"]?)([^\s'\"<>,;]{6,})\3"
)
_AUTH_HEADER = re.compile(
    r"(?i)\b((?:proxy-)?authorization|set-cookie|cookie|x-api-key|x-auth-token)(\s*[:=]\s*)"
    r"(?:(?:basic|bearer|token|digest)\s+)?[^\s'\"]{6,}"
)
_URL_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|client[_-]?secret|key|password|"
    r"passwd|pwd|sig|signature)=)([^&\s\"']+)"
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

#: `the DB password is hunter2hunter2`, `deploy token was 9f8e...`, `the key is now set to 'abc'`: a credential
#: named in a sentence. Group 1 is the words up to the value, 2 the opening quote (or ''), 3 the value with any
#: trailing punctuation; _prose_secret decides whether it is one.
_PROSE = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:pass(?:word|wd|phrase)|pwd|secret|token|api[ _-]?key|apikey|(?:private|access)[ _-]?key"
    r"|credentials?|key)\s+(?:(?:is|was)(?:\s+(?:now|set\s+to|changed\s+to))?|equals|set\s+to)\s+)"
    r"(['\"`]?)([^\s'\"`<>,;]{6,})"
)

#: A bare 32+ character run of letters and digits (hex digest, base62 token). A git SHA has the same shape, which is
#: why scrub_text applies it only on request.
_OPAQUE_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{32,}(?![A-Za-z0-9])")


def _prose_secret(m: re.Match[str]) -> str:
    value = m.group(3)
    core = value.rstrip(".!?)")
    # a word is a value only when quoted or when it has a digit: "the password is stored in the vault" stays a sentence
    if len(core) < 6 or not (m.group(2) or any(c.isdigit() for c in core)) or core == REDACTED:
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}{REDACTED}{value[len(core) :]}"


def _opaque_run(m: re.Match[str]) -> str:
    v = m.group(0)
    return REDACTED if any(c.isdigit() for c in v) and any(c.isalpha() for c in v) else v


#: A bare `--key` hides its value only when that is token-shaped (`sort --key=2,2`, `--key docs/a.txt` stay).
_KEY_FLAG = re.compile(r"(?i)--?key")
_KEY_FLAG_VALUE = re.compile(r"(?i)((?:^|\s)--?key(?:=|\s+))([^\s'\"-][^\s'\"]*)")
#: The scheme starts at the last word start before `://` (an earlier one leaves the same text: only what follows `://`
#: is scrubbed); scanning from every word start of a long run to its end was quadratic (K3NOX-356).
_URL = re.compile(r"\b[a-z](?:[a-z0-9]|[+.\-](?![a-z]))*+://[^\s'\"<>]+", re.I)
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_\-+=~]+")
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_HEX = re.compile(r"[0-9a-fA-F]+")
_SLUG_WORD = re.compile(r"[a-z]+[0-9]{0,4}|[0-9]{1,8}")
#: A value made only of glob characters is a permission pattern (`* --pat *`, `curl *?sig=*`), not a credential:
#: hiding it turns an exported deny rule into one that never matches.
_GLOB = re.compile(r"[*?]+")
#: The user name of a URL can be the credential (`https://<token>@host`; pip prints `https://<token>:****@host`): it is
#: hidden when token-shaped whatever follows it, also a glob-only password that keeps `https://*:*@*` a pattern.
_URL_USER = re.compile(r"(?<=://)[^/\s:@]+(?=(?::[^/\s@]*)?@)")


def _hide(value: str) -> str:
    return value if _GLOB.fullmatch(value) else REDACTED


def scrub_text(text: str, *, opaque: bool = False) -> str:
    """Free text (session messages, URLs, command lines): mask credential shapes.

    Well-known prefixed tokens, private key blocks, URL user info (a password, a token-shaped user name),
    ``NAME_PASSWORD=value`` style assignments, ``Authorization`` /
    ``Cookie`` header values, URL query secrets (``?api_key=...``), ``--password value`` style flags and a credential
    named in a sentence (``the DB password is hunter2hunter2``).

    ``opaque=True`` also masks any bare run of 32+ letters and digits that has both. Text that is about to leave the
    machine or be remembered (a memory fact, the review transcript) asks for it; a git SHA matches too, so logs and
    exports that should keep SHAs do not.
    """
    text = _PEM.sub(REDACTED, text)
    text = _hide_prefixed(text)
    text = _URL_USER.sub(lambda m: REDACTED if _token_shaped(m.group(0), 20) else m.group(0), text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{_hide(m.group(4))}{m.group(3)}", text)
    text = _PROSE.sub(_prose_secret, text)
    text = _URL_SECRET.sub(lambda m: f"{m.group(1)}{_hide(m.group(2))}", text)
    text = _FLAG_SECRET.sub(lambda m: f"{m.group(1)}{_hide(m.group(2))}", text)
    text = _KEY_FLAG_VALUE.sub(lambda m: m.group(1) + (REDACTED if _token_shaped(m.group(2), 8) else m.group(2)), text)
    return _OPAQUE_RUN.sub(_opaque_run, text) if opaque else text


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
        name = REDACTED if _token_shaped(name, 20) else name  # whatever follows it (`<token>:****`)
        authority = f"{name}:{_hide(pw)}@{host}" if colon else f"{name}@{host}"
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


class _OverBudget(Exception):
    pass


def _lead(value: str, text: str, left: list[int]) -> int:
    """The length of the longest suffix of ``value`` that ``text`` starts with; each try spends from ``left``."""
    i = value.find(text[0]) if text else -1
    while i != -1:
        left[0] -= 64 + len(value) - i
        if left[0] < 0:
            raise _OverBudget
        if text.startswith(value[i:]):
            return len(value) - i
        i = value.find(text[0], i + 1)
    return 0


def drop_after_shapes(shaped: str, original: str, values: list[str]) -> str:
    """``shaped`` (``original`` through the known-value and shape passes) without ``values``: each whole, and what is
    left of one those passes cut (a shape at a `,`, a quote or a space, a known value inside it): the part of it
    beside, or between two of, their ``REDACTED``. For the values known only from a file the user may never have
    trusted: dropped before the shape pass (``drop_values``), one could blank the word a shape needs (`Bearer`,
    `token=`) and leave the credential after it in clear.

    The search for what is left grows with the values and the markers: past a budget in proportion to the text,
    the whole text is hidden instead (a file can name many long values and a session can hold many markers)."""
    present = [v for v in values if v and v in original]
    text = shaped
    for v in present:
        text = text.replace(v, REDACTED)
    cut = [v for v in present if shaped.count(v) < original.count(v)]
    if not cut:
        return text
    parts = text.split(REDACTED)
    left = [16 * len(original) + 65_536 - 24 * len(cut) * len(parts)]  # every value beside every marker, three times
    if left[0] < 0:
        return REDACTED
    try:
        for v in cut:
            for n in range(1, len(parts) - 1):
                if parts[n] in v:
                    parts[n] = ""
            for n in range(1, len(parts)):
                parts[n] = parts[n][_lead(v, parts[n][: len(v)], left) :]
            for n in range(len(parts) - 1):
                tail = parts[n][-len(v) :]
                parts[n] = parts[n][: len(parts[n]) - _lead(v[::-1], tail[::-1], left)]
    except _OverBudget:
        return REDACTED
    return REDACTED.join(parts)


def _flag_value(flag: str, value: str) -> bool:
    """``value`` follows ``flag`` in an argv list and is its credential (after a bare ``--key``: if token-shaped)."""
    return bool(_SECRET_FLAG.fullmatch(flag)) or (bool(_KEY_FLAG.fullmatch(flag)) and _token_shaped(value, 8))


def redact(obj: Any, *, _force: bool = False, _argv: bool = False) -> Any:
    """Deep copy of ``obj`` with secrets replaced by ``"<redacted>"``."""
    if isinstance(obj, dict):
        return {  # a key can carry a secret too (`permissions.bash` stores an approved command as the key)
            redact(k) if isinstance(k, str) else k: redact(
                v, _force=_force or is_secret_key(k) or str(k).lower() in _SECRET_SECTIONS, _argv=k == "args"
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
            out.append(redact(v, _force=_force))
        return out
    if isinstance(obj, str):
        if obj == "":
            return obj
        # not a secret by name or shape: it can still carry one (a URL with ?api_key=, an arg list with --token x)
        return REDACTED if _force or looks_like_secret(obj) else scrub_text(scrub_urls(obj))
    return obj


def redact_session_json(obj: Any, known: list[str] | None = None, untrusted: list[str] | None = None) -> Any:
    """Session payloads: in every string the ``known`` secret values first (``drop_values``: the shape pass can cut
    one at a `,` or a quote and leave its tail behind), then the credential shapes, then the ``untrusted`` values
    (``drop_after_shapes``). A key, which has no shape pass, loses both kinds."""
    known, untrusted = known or [], untrusted or []
    if isinstance(obj, dict):
        return {
            drop_values(drop_values(k, known), untrusted): redact_session_json(v, known, untrusted)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_session_json(v, known, untrusted) for v in obj]
    if isinstance(obj, str):
        shaped = scrub_text(drop_values(obj, known))
        return drop_after_shapes(shaped, obj, untrusted) if untrusted else shaped
    return obj
