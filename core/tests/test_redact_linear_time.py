"""Redaction and the failure signature run on whatever a tool printed: one long line must not stall the loop.

Each pattern below used to restart at every word start inside a long run of name characters and scan to the end of
the run (quadratic): 8 KB of `-a1_` took seconds in scrub_text, a 16 KB tool-error line most of a minute in
failure_of, which runs on the agent loop (K3NOX-356). The new patterns must still hide what the old ones hid.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from k3code import redact, toolerrors
from k3code.redact import REDACTED, looks_like_secret, scrub_text, scrub_urls
from k3code.toolerrors import failure_of, normalise

# Fake credentials are built at run time: the repo's secret scan rejects a literal that looks real.
PASSWORD = "hunter2" * 2
JWT = ".".join(("eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "SflKxwRJSMeKKF2" + "QT4fwpMeJf36"))

#: 64 KB lines, each quadratic for one of the patterns before.
LONG_LINES = {
    "dash run": "--" + "a-" * 32768,  # _ASSIGNMENT: a word start at every `a`
    "mixed run": "-a1_" * 16384,
    "dotted run": "a." * 32768,
    "flag run": " --" + "a-" * 32768,
    "keyword run": "token" * 13108,  # _ASSIGNMENT: one start, a keyword every 5 characters
    "header word run": "authorization" * 5042,
    "jwt starts": "eyJ" * 21846,  # _PREFIXED: a JWT start every 3 characters
    "scheme run": "sk-" * 21846,  # scrub_urls: a scheme start at every `s`
    "no slash": "a" * 65536,  # toolerrors._PATH: a path start at every character
    "tilde run": "a~" * 32768,
    "glued jwt starts": "xeyJ" * 16384,
    "jwt with a long head": "x" + "eyJ" * 21845 + ".abcdefgh.abcd",
}


def _took(fn, text: str) -> float:
    start = time.perf_counter()
    fn(text)
    return time.perf_counter() - start


def _bash_failure(stderr: str):
    return failure_of("bash", {"command": "make"}, {"exit_code": 1, "stderr": stderr})


@pytest.mark.parametrize("name", LONG_LINES)
def test_one_long_line_is_scrubbed_in_linear_time(name):
    text = LONG_LINES[name]
    assert _took(scrub_text, text) < 1.0
    assert _took(lambda t: scrub_text(t, opaque=True), text) < 1.0
    assert _took(scrub_urls, text) < 1.0
    assert _took(looks_like_secret, text) < 1.0
    assert _took(_bash_failure, text) < 1.0


@pytest.mark.parametrize("name", LONG_LINES)
def test_a_credential_after_a_long_line_is_still_hidden(name):
    filler = LONG_LINES[name]
    out = scrub_text(f"{filler} DB_PASSWORD={PASSWORD} jwt {JWT}")
    assert PASSWORD not in out and JWT not in out and out.endswith(f"DB_PASSWORD={REDACTED} jwt {REDACTED}")
    out = scrub_urls(f"{filler} https://deploy:{PASSWORD}@example.com/x")
    assert PASSWORD not in out and out.endswith(f"https://deploy:{REDACTED}@example.com/x")
    failure = _bash_failure(f"error: token={PASSWORD} {filler}")
    assert failure is not None and PASSWORD not in failure.first_line + failure.signature
    assert failure.first_line.startswith(f"make: exit 1: error: token={REDACTED}")


def test_a_name_glued_to_a_long_run_still_hides_its_value():
    assert scrub_text("a-" * 30000 + f"DB_PASSWORD={PASSWORD}").endswith(f"DB_PASSWORD={REDACTED}")
    assert scrub_text("." * 30000 + f"api_key: '{PASSWORD}'").endswith(f"api_key: '{REDACTED}'")


def test_a_name_after_a_non_ascii_letter_is_read_as_before():
    # a name starts at a word boundary, as before: there is none between `é` and the letter after it, so the assignment
    # is the one after `=` (matching from `é` hid the next name and left the value in clear, K3NOX-685)
    assert scrub_text(f"éfoo_token=DB_PASSWORD = {PASSWORD}") == f"éfoo_token=DB_PASSWORD = {REDACTED}"


def test_a_jwt_glued_to_a_word_is_hidden_from_its_first_eyj():
    assert scrub_text(f"x{JWT} rest") == f"x{REDACTED} rest"
    assert scrub_text(f"AKIA{'ABCDEFGHIJKLMNOP'}{JWT}") == REDACTED * 2
    # a header that holds another `eyJ` (a nested object) is hidden whole, glued or not
    nested = "eyJ" + "hbGciOiJIUzI1NiJ9" + "eyJ" + "raWQiOiIxIn0" + JWT[JWT.index(".") :]
    assert scrub_text(f"jwt: {nested}") == f"jwt: {REDACTED}"
    assert scrub_text(f"x{nested}") == f"x{REDACTED}"
    # a first part too short for `eyJ` and 8 more is no JWT, whatever follows (an `eyJ` in the third part is not one)
    assert redact._prefixed_spans("ab." + "x" * 10 + ".eyJ" + "y" * 9) == []


#: Prefixed tokens a JWT's first part can hold (each long enough for its own rule), built at run time.
_INNER_TOKENS = {
    "sk": "sk-" + "a1" * 16,
    "glpat": "glpat-" + "b2" * 10,
    "aiza": "AIza" + "c3" * 18,
    "ghp": "ghp_" + "d4" * 12,
    "xox": "xoxb-" + "e5" * 8,
    "akia": "AKIA" + "F6" * 8,
}


@pytest.mark.parametrize("glue", ["x", "a_", "id-", "9", "é", ""])
@pytest.mark.parametrize("inner", _INNER_TOKENS)
def test_a_glued_jwt_with_a_token_inside_is_hidden_from_its_first_eyj(glue, inner):
    # `<glue>eyJ<8><token>eyJ<8>.<payload>.<sig>`: starting at the last `eyJ` let the token's rule swallow it and left
    # the payload and the signature in clear (K3NOX-685); the JWT starts at its first `eyJ`, as it did before
    tail = JWT[JWT.index(".") :]
    text = f"{glue}eyJ{'hbGciOiJ'}{_INNER_TOKENS[inner]}eyJ{'raWQiOiI'}{tail}"
    assert scrub_text(text) == f"{glue}{REDACTED}"
    assert redact._prefixed_spans(text) == [m.span() for m in _OLD_PREFIXED.finditer(text)]


# The patterns as they were before K3NOX-356: on short text, where the old cost does not matter, the new ones must
# give the same result.
_OLD_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_.\-]*(?:password|passwd|secret|token|api[_-]?key|apikey|private[_-]?key|credential|"
    r"access[_-]?key|auth)[A-Z0-9_.\-]*)(\s*[=:]\s*)(['\"]?)([^\s'\"<>,;]{6,})\3"
)
_OLD_PREFIXED = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{12,}|sk_(?:live|test)_[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|glpat-[A-Za-z0-9_\-]{16,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}|Bearer\s+[A-Za-z0-9._\-~+/]{16,}"
    r"|Bearer\s+(?=[A-Za-z0-9._\-~+/]*[0-9._\-~+/])[A-Za-z0-9._\-~+/]{6,}"
    r"|://[^/\s:@]+:(?![*?]+@)[^/\s@]+@)"
)
_OLD_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"<>]+", re.I)
_OLD_PATH = re.compile(r"(?:[A-Za-z0-9_.~\-]*/)+[A-Za-z0-9_.\-]*")


def _samples(pieces: list[str], n: int = 3000, seed: int = 356) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(pieces) for _ in range(rng.randint(1, 24))) for _ in range(n)]


def _same_with(monkeypatch, module, name: str, old: re.Pattern[str], fn, pieces: list[str]) -> None:
    for text in _samples(pieces):
        new_out = fn(text)
        with monkeypatch.context() as m:
            m.setattr(module, name, old)
            old_out = fn(text)
        assert new_out == old_out, text


def test_assignments_are_hidden_as_before(monkeypatch):
    pieces = ["token", "DB_", "PASSWORD", "auth", "api-key", "a", "1", "_", "-", ".", "=", ":", " ", "'", '"', ",", "x"]
    pieces += ["abcdef", "s3cr3t-v4lue", "<", "\t", "é", "ä", "²", "/"]
    _same_with(monkeypatch, redact, "_ASSIGNMENT", _OLD_ASSIGNMENT, scrub_text, pieces)


def test_prefixed_tokens_and_jwts_are_found_as_before(monkeypatch):
    pieces = ["eyJ", "eyJabcdefgh", " eyJ", ".eyJ", "abcdefgh", "abcd", ".", " ", "-", "_", "x", "é", "Bearer "]
    pieces += ["://u", ":pw@", "x1", *_INNER_TOKENS.values(), "sk-", "AKIA", "ABCDEFGHIJKLMNOP", "ghp_", "glpat-"]
    for text in _samples(pieces, n=6000):
        assert redact._prefixed_spans(text) == [m.span() for m in _OLD_PREFIXED.finditer(text)], text
        new_secret, new_out = looks_like_secret(text), scrub_text(text)
        with monkeypatch.context() as m:  # the old regex, JWT rule included, and no separate JWT scan
            m.setattr(redact, "_PREFIXED", _OLD_PREFIXED)
            m.setattr(redact, "_jwts", lambda _text: iter(()))
            assert (new_secret, new_out) == (looks_like_secret(text), scrub_text(text)), text


def test_urls_are_scrubbed_as_before(monkeypatch):
    pieces = ["http", "s", "a", "1", "+", ".", "-", "_", "://", "u:", "pw12345678@", "h/", "?k=", "x" * 8, " ", "é"]
    pieces += ["'", "tok3nVALUE12345678901234/"]
    _same_with(monkeypatch, redact, "_URL", _OLD_URL, scrub_urls, pieces)


def test_failure_signatures_are_normalised_as_before(monkeypatch):
    pieces = ["a", "1", "~", "/", ".", "-", "_", " ", ":", "src", "x.py", "é"]
    _same_with(monkeypatch, toolerrors, "_PATH", _OLD_PATH, normalise, pieces)
