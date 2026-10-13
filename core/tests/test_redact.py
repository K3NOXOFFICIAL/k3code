"""What leaves the machine in an export bundle or a debug dump must not carry credentials."""

from __future__ import annotations

import pytest

from k3code.redact import REDACTED, redact, redact_session_json, scrub_text

# Fake credentials for the prose cases are built at run time: the repo's secret scan rejects a literal that looks real.
PASSWORD = "hunter2" * 2
HEX_TOKEN = "".join(("9f8e7d6c", "5b4a3928", "1706f5e4", "d3c2b1a0"))
API_KEY = "sk" + "1234567890abcdef"
AWS_KEY = "wJalrXUtn" + "FEMI7K7MDENG"
GIT_SHA = "a94a8fe5ccb19ba6" + "1c4c0873d391e987982fbbd3"


@pytest.mark.parametrize(
    "text, secret",
    [
        ("DB_PASSWORD=hunter2secret", "hunter2secret"),
        ("export SECRET_KEY='abcd1234efgh'", "abcd1234efgh"),
        ("client_secret: s3cr3t-value-xyz", "s3cr3t-value-xyz"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
        ("authorization: Bearer abcdef0123456789", "abcdef0123456789"),
        ("Cookie: sessionid=abc123def456; theme=dark", "abc123def456"),
        ("curl 'https://api.example.com/v1/x?api_key=KEY123456&limit=5'", "KEY123456"),
        ("https://host/path?token=tok_abcdef123&x=1", "tok_abcdef123"),
        ("mycli --password correct-horse-battery", "correct-horse-battery"),
        ("mycli --api-key=AbCdEf123456 run", "AbCdEf123456"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----", "b3BlbnNzaC1rZXk"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghij"),
    ],
)
def test_free_text_credentials_are_scrubbed(text, secret):
    out = scrub_text(text)
    assert secret not in out and REDACTED in out, out


@pytest.mark.parametrize(
    "text, secret",
    [
        (f"The user's AWS password is {PASSWORD} for the DB", PASSWORD),
        (f"deploy token is {HEX_TOKEN}", HEX_TOKEN),
        (f"the api key was {API_KEY}.", API_KEY),
        ("Secret is 'correcthorse'", "correcthorse"),
        ("the staging password was set to Tr0ub4dor&3 yesterday", "Tr0ub4dor&3"),
        (f"AWS_SECRET_KEY is {AWS_KEY}", AWS_KEY),
    ],
)
def test_a_credential_stated_in_prose_is_scrubbed(text, secret):
    out = scrub_text(text)
    assert secret not in out and REDACTED in out, out


def test_the_prose_rule_keeps_the_sentence_around_the_value():
    assert scrub_text(f"The password is {PASSWORD}. Rotate it.") == f"The password is {REDACTED}. Rotate it."


@pytest.mark.parametrize(
    "text",
    [
        "the author wrote a long paragraph about authentication in general",
        "set the timeout to 30 seconds and retry",
        "git log --oneline --author=alice",
        "tokens per second: 1234; keyboard layout: us",
        "see https://example.com/docs?page=2&sort=asc",
        "PASSWORD_MIN_LENGTH=",  # nothing to hide
        "the password is stored in the vault",  # no digit: a sentence, not a value
        "the key is to run the tests first",
        "the token is 42",  # too short to be a credential
        "monkey is banana12345",  # `key` inside a word
        f"see commit 934ae07 and the 40-char hash {GIT_SHA}",  # opaque only on request
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert scrub_text(text) == text


def test_a_standalone_long_token_is_scrubbed_only_when_asked():
    text = f"log in with {HEX_TOKEN} on the box"
    assert scrub_text(text) == text  # a git SHA looks the same: session exports and tool errors keep them
    out = scrub_text(text, opaque=True)
    assert HEX_TOKEN not in out and out == f"log in with {REDACTED} on the box"


@pytest.mark.parametrize(
    "text",
    [
        "LongCamelCaseIdentifierWithoutDigitsAtAllInIt",  # no digit
        "12345678901234567890123456789012",  # no letter
        "x" * 40,
        "short9f8e7d6c5b4a",  # under 32
        "k3nox-224-prose-secret-scrub-in-the-memory-area",  # a slug: the rule is alnum runs only
        "tests/test_learning_review_curator_for_python_v3.py",
    ],
)
def test_the_standalone_token_rule_leaves_other_long_words_alone(text):
    assert scrub_text(text, opaque=True) == text


def test_mcp_server_env_headers_and_args_are_redacted_by_default():
    cfg = {
        "mcp": {
            "servers": {
                "gateway": {
                    "url": "https://mcp.example.com/mcp?api_key=SECRETVALUE1",
                    "headers": {"Authorization": "Basic Zm9vOmJhcg==", "X-Custom": "abc"},
                    "env": {"SOME_VAR": "plain-looking-value", "PATH": "/usr/bin"},
                    "args": ["--token", "tok_12345678", "--port", "80"],
                    "command": "npx",
                }
            }
        },
        "providers": [{"name": "p", "api_key_env": "MY_KEY_ENV", "base_url": "https://p/v1"}],
        "cookie_jar": "abc",
        "author": "alice",
    }
    out = redact(cfg)
    srv = out["mcp"]["servers"]["gateway"]
    assert "SECRETVALUE1" not in srv["url"] and set(srv["headers"].values()) == {REDACTED}
    assert set(srv["env"].values()) == {REDACTED}
    assert "tok_12345678" not in " ".join(srv["args"]) and "80" in srv["args"]
    assert srv["command"] == "npx"
    assert out["providers"][0]["api_key_env"] == "MY_KEY_ENV"  # the env var *name* is not a secret
    assert out["cookie_jar"] == REDACTED and out["author"] == "alice"


def test_session_payloads_are_scrubbed_in_every_string():
    session = {"messages": [{"role": "tool", "content": "$ cat .env\nDB_PASSWORD=hunter2secret\nAPI_KEY=abcdef123456"}]}
    out = redact_session_json(session)
    text = out["messages"][0]["content"]
    assert "hunter2secret" not in text and "abcdef123456" not in text and "$ cat .env" in text
