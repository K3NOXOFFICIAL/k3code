"""What leaves the machine in an export bundle or a debug dump must not carry credentials."""

from __future__ import annotations

import pytest

from k3code.redact import REDACTED, redact, redact_session_json, scrub_text


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
    "text",
    [
        "the author wrote a long paragraph about authentication in general",
        "set the timeout to 30 seconds and retry",
        "git log --oneline --author=alice",
        "tokens per second: 1234; keyboard layout: us",
        "see https://example.com/docs?page=2&sort=asc",
        "PASSWORD_MIN_LENGTH=",  # nothing to hide
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert scrub_text(text) == text


def test_mcp_server_env_headers_and_args_are_redacted_by_default():
    cfg = {
        "mcp": {
            "servers": {
                "k3nox": {
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
        "author": "the owner",
    }
    out = redact(cfg)
    srv = out["mcp"]["servers"]["k3nox"]
    assert "SECRETVALUE1" not in srv["url"] and set(srv["headers"].values()) == {REDACTED}
    assert set(srv["env"].values()) == {REDACTED}
    assert "tok_12345678" not in " ".join(srv["args"]) and "80" in srv["args"]
    assert srv["command"] == "npx"
    assert out["providers"][0]["api_key_env"] == "MY_KEY_ENV"  # the env var *name* is not a secret
    assert out["cookie_jar"] == REDACTED and out["author"] == "the owner"


def test_session_payloads_are_scrubbed_in_every_string():
    session = {"messages": [{"role": "tool", "content": "$ cat .env\nDB_PASSWORD=hunter2secret\nAPI_KEY=abcdef123456"}]}
    out = redact_session_json(session)
    text = out["messages"][0]["content"]
    assert "hunter2secret" not in text and "abcdef123456" not in text and "$ cat .env" in text
