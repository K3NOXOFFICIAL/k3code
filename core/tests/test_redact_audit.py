"""Redaction audit: credential shapes ``scrub_text`` used to let through, the ordinary text it must leave alone, and
dict keys (a permission rule such as ``npx tool --token sk-...`` is a key, and keys reach ``config.get`` and bundles).

Every secret below is an obvious fake."""

from __future__ import annotations

import pytest

from k3code.redact import REDACTED, redact, redact_session_json, scrub_text

HF = "hf_" + "abcdefghijklmnopqrstuvwxyz01234567"
NPM = "npm_" + "abcdefghijklmnopqrstuvwxyz0123456789"
ASIA = "ASIA" + "0123456789ABCDEF"
SENDGRID = "SG." + "abcdefghijklmnopqrstuv" + "." + "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
# assembled at run time: a literal webhook URL in the repository is refused by the host's push protection
SLACK = "https://hooks.slack" + ".com/services/" + "T00000000" + "/" + "B00000000" + "/" + "X" * 24
DISCORD = "https://discord" + ".com/api/webhooks/" + "0" * 18 + "/" + "FAKEfakeFAKEfake0000-_abc"
# every fake secret is assembled at run time so that no secret-shaped literal sits in the repository (secret scan)
PGP = (
    "-----BEGIN "
    + "PGP PRIVATE KEY BLOCK-----\n\nlQVYBFAKEFAKEFAKEFAKE\n=abcd\n-----END "
    + "PGP PRIVATE KEY BLOCK-----"
)
API_KEY_JSON = '{"api' + '_key": "' + "abc123" + 'def456ghi"}'
ACCESS_TOKEN_JSON = '{"access' + '_token": "' + "tok-" + '0123456789abcdef"}'
REFRESH_URL = "https://x.example.com/?refresh" + "_token=" + "rt01234567" + "89abcdef"


@pytest.mark.parametrize(
    "text, secret",
    [
        # JSON / quoted keys, values with spaces
        (API_KEY_JSON, "abc123def456ghi"),
        (ACCESS_TOKEN_JSON, "tok-0123456789abcdef"),
        ('{"password": "correct horse battery"}', "horse"),
        ("{'password': 'hunter22secret'}", "hunter22secret"),
        ('{"client_secret":"s3cr3t-value-xyz"}', "s3cr3t-value-xyz"),
        ('{"Authorization": "Basic dXNlcjpwYXNzd29yZA=="}', "dXNlcjpwYXNzd29yZA"),
        ('{"Cookie": "sid=abc123def456; theme=dark; other=value99"}', "other=value99"),
        ("Cookie: sid=abc123def456; theme=dark; other=value99", "other=value99"),
        # short names
        ("DB_PASS=hunter22secret", "hunter22secret"),
        ("export PASS='hunter22secret'", "hunter22secret"),
        ("Server=db;Uid=app;Pwd=hunter22secret;Database=x", "hunter22secret"),
        ("pwd=hunter22secret", "hunter22secret"),
        ('{"pwd": "hunter22secret"}', "hunter22secret"),
        # command lines
        ("mysql -uroot -phunter22secret mydb", "hunter22secret"),
        ("mysqldump -h db -u root -p'hunter 22 secret' mydb", "hunter 22"),
        ("sshpass -p hunter22secret ssh host", "hunter22secret"),
        ("sshpass -p 'hunter 22 secret' ssh host", "hunter 22"),
        ("curl -u admin:hunter22secret https://example.com/x", "hunter22secret"),
        ("curl -s https://example.com/x?a=1&b=2 -u admin:hunter22secret", "hunter22secret"),
        ("curl --user admin:hunter22secret https://example.com/x", "hunter22secret"),
        ("curl -sSu admin:hunter22secret https://example.com/x", "hunter22secret"),
        ("curl -U proxyuser:hunter22secret https://example.com/x", "hunter22secret"),
        ("curl --user=admin:hunter22secret https://example.com/x", "hunter22secret"),
        ("curl -u 'admin:hunter 22 secret' https://example.com/x", "hunter 22"),
        # token shapes
        (f"HF_TOKEN={HF}", HF),
        (f"token is {HF} ok", HF),
        (f"//registry.npmjs.org/:_authToken={NPM}", NPM),
        (f"aws key {ASIA} in logs", ASIA),
        (f"SENDGRID_API_KEY: {SENDGRID}", SENDGRID),
        ("stripe pk_live_" + "a1b2c3d4e5f6g7h8i9", "pk_live_a1b2"),
        ("stripe sk_live_" + "a1b2c3d4e5f6g7h8i9", "sk_live_a1b2"),
        ("stripe rk_test_" + "a1b2c3d4e5f6g7h8i9", "rk_test_a1b2"),
        # webhook URLs: the path is the credential
        (f"post to {SLACK} now", "XXXXXXXXXXXX"),
        (f"post to {DISCORD} now", "FAKEfakeFAKEfake"),
        # URL secrets
        ("https://app.example.com/cb?code=4%2F0AfakeOAuthCode0123456789&state=xyz", "0AfakeOAuthCode"),
        ("https://app.example.com/cb#access_token=tok0123456789abc&type=bearer", "tok0123456789abc"),
        (
            "https://b.s3.amazonaws.com/o?X-Amz-Signature=" + "0123456789abcdef" * 4 + "&X-Amz-Expires=60",
            "0123456789abcdef",
        ),
        ("https://b.s3.amazonaws.com/o?X-Amz-Security-Token=FwoGZXIvYXdz0000000000&a=1", "FwoGZXIvYXdz0000000000"),
        (REFRESH_URL, "rt0123456789abcdef"),
        # PGP
        (PGP, "lQVYBFAKE"),
    ],
)
def test_more_credential_shapes_are_scrubbed(text, secret):
    out = scrub_text(text)
    assert secret not in out and REDACTED in out, out


@pytest.mark.parametrize(
    "text",
    [
        "a password policy needs twelve characters",
        "the password: see the vault",
        "password policy: at least 12 characters",
        "run pwd to print the working directory",
        "$ pwd\n/home/user/project",
        "PWD=/home/user/project",
        "OLDPWD=/home/user/other-project",
        "cwd: /home/user/project and pwd are the same",
        "compass=northwest-direction and bypass=1234567890",
        'git log --author="Jane Doe" --oneline',
        'author: "Jane Doe"',
        '{"tokenizer": "gpt2", "authors": ["a b"]}',
        "Set-Cookie: a; Path=/",
        "load hf_model_name and hf_tokenizer_config_file from the hub",
        "set npm_config_cache to a folder; npm_package_name is read-only",
        "https://api.example.com/status?code=200",
        "https://api.example.com/status?code=404&retry=1",
        "https://example.com/search?code=ABC&x=1",
        "use curl -u to pass credentials, or curl --user",
        "curl -u admin https://example.com/x",
        "curl -s -o /dev/null https://example.com/x",
        "mysql -p",
        "mysql -h db -P 3306 -u root -p",
        "mysql --port=3306 -h db mydb",
        "mysqldump --single-transaction db > dump.sql",
        "ssh -p 2222 host",
        "sshpass -e ssh host",
        "python -m http.server -p 8080",
        "ASIAN markets and ASIA PACIFIC",
        "ASIA" + "PACIFICREGIONNAMEISLONG",
        "see https://hooks.slack.com/ and https://discord.com/api/webhooks for the docs",
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQENBFAKEPUBLIC\n-----END PGP PUBLIC KEY BLOCK-----",
        '{"max_tokens": 100000, "model": "claude", "count": 5}',
        '{"name": "test", "tokens_used": 1234567, "authored": true}',
        "SG.short and SG.x.y",
        "pk_live_stream and sk_live_ is a prefix",
    ],
)
def test_ordinary_text_is_still_left_alone(text):
    assert scrub_text(text) == text


def test_scrubbing_is_idempotent_on_the_new_shapes():
    text = f'{{"password": "a b c d"}} mysql -phunter22secret {SLACK} Cookie: a=b123456; c=d123456'
    once = scrub_text(text)
    assert scrub_text(once) == once


def test_new_prefixed_tokens_are_secrets_as_whole_values():
    for token in (HF, NPM, ASIA, SENDGRID, SLACK, DISCORD):
        assert redact({"note": token}) == {"note": REDACTED}, token


# ── dict keys ──


def test_secrets_in_dict_keys_are_scrubbed():
    cfg = {
        "permissions": {
            "bash": {
                "npx tool --token sk-test0000000000000000": "allow",
                "curl -u admin:hunter22secret https://example.com": "allow",
                "git push *": "allow",
            }
        },
        "api_key_env": "MY_KEY_ENV",
        7: "kept",
    }
    out = redact(cfg)
    rules = out["permissions"]["bash"]
    assert "sk-test0000000000000000" not in "".join(rules)
    assert "hunter22secret" not in "".join(rules)
    assert "git push *" in rules and rules["git push *"] == "allow"
    assert out["api_key_env"] == "MY_KEY_ENV" and out[7] == "kept"


def test_a_secret_named_key_still_hides_its_value_after_the_key_is_scrubbed():
    out = redact({"token --x sk-test0000000000000000": "value-that-is-not-a-secret"})
    assert list(out.values()) == [REDACTED]
    assert "sk-test0000000000000000" not in "".join(out)


def test_session_json_keys_are_scrubbed_too():
    out = redact_session_json({"files": {"curl -u admin:hunter22secret x": "ok", "a.py": "x = 1"}})
    assert "hunter22secret" not in "".join(out["files"]) and out["files"]["a.py"] == "x = 1"


def test_ordinary_keys_are_untouched():
    cfg = {"git log --oneline": "allow", "providers": [{"name": "p"}], "a b c": {"d.e": 1}}
    assert redact(cfg) == cfg


@pytest.mark.parametrize("text", ["a." * 5000, "ab-" * 20000, "PASS_" * 5000, "mysql " * 2000, "curl -x " * 2000])
def test_long_runs_of_name_characters_are_scrubbed_in_linear_time(text):
    """The name pattern was quadratic: 10 KB of ``a.a.a.`` took seconds (an export stalled on such a line)."""
    import time

    start = time.monotonic()
    assert scrub_text(text) == text
    assert time.monotonic() - start < 2.0


def test_the_curl_password_scrub_is_linear() -> None:
    # K3NOX-106: an unterminated quote full of colons after `curl -u` backtracked once per colon (27 s at 20 KB); the
    # first stderr line of a failing command is scrubbed on the event loop
    import time

    started = time.perf_counter()
    for text in ("curl " * 50 + '-u "' + "a:" * 10_000, "curl -u '" + "a:" * 10_000, "curl -u " + "a:" * 10_000):
        scrub_text(text)
    assert time.perf_counter() - started < 1.0
    assert "hunter22" not in scrub_text('curl -u "admin:hunter22" https://x')  # quoted forms still masked
    assert "hunter22" not in scrub_text("curl -u 'admin:a:hunter22' https://x")


@pytest.mark.parametrize(
    "rule",
    ["curl -u *:* *", "curl --user *:*", "mysql -p* -h *", "sshpass -p * ssh *", "curl -u user:?? *"],
)
def test_a_glob_only_command_line_password_is_a_pattern_not_a_credential(rule: str) -> None:
    # a deny rule exported with a glob where the password sits must still match after an import
    assert scrub_text(rule) == rule
    assert redact({"permissions": {"bash": {rule: "deny"}}}) == {"permissions": {"bash": {rule: "deny"}}}


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("curl -u admin:hunter22pw https://x", "hunter22pw"),
        ("curl -u 'admin:*hunter22pw' https://x", "hunter22pw"),
        ("mysql -h db -phunter22pw", "hunter22pw"),
        ("sshpass -p hunter22pw ssh host", "hunter22pw"),
        ("curl https://x/?code=hunter22pwcode", "hunter22pwcode"),
    ],
)
def test_a_command_line_password_that_is_not_only_glob_characters_is_still_hidden(text: str, secret: str) -> None:
    assert secret not in scrub_text(text)
