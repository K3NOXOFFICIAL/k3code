"""The /debug dump bundle is meant to be attached to bug reports: no file in it may carry a credential."""

from __future__ import annotations

import json
import tarfile

import pytest

from k3code import debugdump
from k3code.config import McpServerConfig, ProviderEntry, Settings
from k3code.redact import REDACTED, redact
from test_gateway import make_server

# Fake credentials, built at run time so the secret scan has nothing to match.
PAT = "ghp_" + "a1B2c3D4e5" * 4
PW = "hunter2" + "pw"
SIG = "ABCDEF" + "123456"
HDR = "zzzz" + "1234"
URL_PW = "s3cret" + "pw"
PATH_TOKEN = "t0k3n" + "InPath" + "Zq81xYw9Pq2LmN"
QUERY_TOKEN = "ABCDEF" + "999999"
ARGV_TOKEN = "Zm9v" + "YmFy" + "QmF6" + "UXV4" + "MTIz"
FLAG_VALUE = "pos1tional" + "Secret42"
EVENT_ONLY = "evt-only-" + "secret-value-4711"


def _config() -> Settings:
    return Settings(
        providers=[
            ProviderEntry(name="p", kind="openai", base_url="http://127.0.0.1:9/v1", api_key_env="K3_X_KEY", models={})
        ],
        mcp={
            "servers": {
                "gh": McpServerConfig(
                    command="npx",
                    args=["-y", "server", "--pat", PAT, f"--password={PW}", "--github-token", FLAG_VALUE, ARGV_TOKEN],
                    env={"GITHUB_PAT": PAT, "DATABASE_URL": f"postgres://u:{PW}@db/x", "PLAIN": EVENT_ONLY},
                ),
                "remote": McpServerConfig(
                    url=f"https://mcp.example.com/mcp/{PATH_TOKEN}/sse?sig={SIG}&foo={QUERY_TOKEN}&limit=5",
                    headers={"X-Custom": HDR},
                ),
            }
        },
        research={"searxng_url": f"https://bob:{URL_PW}@searx.example.com"},
    )


def _bundle_text(path) -> dict[str, str]:
    with tarfile.open(path) as tar:
        return {n: tar.extractfile(n).read().decode() for n in tar.getnames()}


def test_safe_config_redacts_mcp_and_research_secrets():
    text = json.dumps(debugdump.safe_config(_config()))
    for secret in (PAT, PW, SIG, HDR, URL_PW, PATH_TOKEN, QUERY_TOKEN, FLAG_VALUE, ARGV_TOKEN, EVENT_ONLY):
        assert secret not in text, secret
    # what is not secret stays readable
    assert "mcp.example.com" in text and "npx" in text and "limit=5" in text


async def test_every_file_of_the_bundle_is_redacted(tmp_path):
    server = make_server(config=_config())
    server.emit(
        "tool_result",
        {
            "output": f"calling https://api.example.com/v1/{PATH_TOKEN}/x and the plain env value {EVENT_ONLY}",
            "headers": {"Authorization": "Basic Zm9vOmJhcg=="},
            "api_token": PW,
        },
    )
    path = await debugdump.write_bundle(server, 50, home=tmp_path, probe=False)
    files = _bundle_text(path)
    assert {"events.json", "config.json", "versions.json", "doctor.json"} <= set(files)
    blob = "\n".join(files.values())
    for secret in (PAT, PW, SIG, HDR, URL_PW, PATH_TOKEN, QUERY_TOKEN, FLAG_VALUE, EVENT_ONLY, "Zm9vOmJhcg=="):
        assert secret not in blob, secret
    assert REDACTED in files["events.json"] and REDACTED in files["config.json"]


async def test_secrets_in_doctor_output_do_not_reach_the_bundle(tmp_path, monkeypatch):
    from k3code import doctor

    async def fake_checks(*_a, **_k):
        return [doctor.Check("probe", "fail", f"GET https://host.example.com/{PATH_TOKEN}?api_key={SIG} failed")]

    monkeypatch.setattr(doctor, "run_checks", fake_checks)
    path = await debugdump.write_bundle(make_server(config=_config()), 5, home=tmp_path, probe=False)
    blob = _bundle_text(path)["doctor.json"]
    assert PATH_TOKEN not in blob and SIG not in blob and "host.example.com" in blob


@pytest.mark.parametrize(
    "value",
    [
        f"https://mcp.example.com/mcp/{PATH_TOKEN}",
        f"https://mcp.example.com/{'ab' * 12}/mcp",  # 24 hex-like characters
        f"https://mcp.example.com/mcp?foo={QUERY_TOKEN}",
        f"https://{PATH_TOKEN}@github.example.com/org/repo",  # token as the user name
    ],
)
def test_token_shaped_url_parts_are_redacted(value):
    out = redact({"url": value})["url"]
    assert out != value and REDACTED in out
    assert "mcp.example.com" in out or "github.example.com" in out  # host stays


@pytest.mark.parametrize(
    "value",
    [
        "https://docs.example.com/guide/getting-started-with-the-api-and-more",
        "https://github.com/K3NOXOFFICIAL/k3code/issues/12?page=2&sort=asc",
        "http://127.0.0.1:8080/v1",
        "ssh://git@github.com/org/repo.git",
    ],
)
def test_ordinary_urls_are_left_alone(value):
    assert redact({"url": value})["url"] == value


def test_secret_flags_hide_the_next_argument_whatever_the_flag_is_called():
    out = redact({"args": ["serve", "--access-token", FLAG_VALUE, "--pat", PAT, "--key=" + PW, "--port", "80"]})
    text = " ".join(out["args"])
    assert FLAG_VALUE not in text and PAT not in text and PW not in text
    assert "serve" in text and "--port 80" in text


def test_a_lone_token_shaped_argument_is_hidden_but_package_names_stay():
    out = redact({"args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/project", ARGV_TOKEN]})["args"]
    assert out[:3] == ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/project"]
    assert out[3] == REDACTED


def test_known_secret_values_are_dropped_from_any_shape(monkeypatch):
    from k3code.redact import drop_values

    out = drop_values({"a": [f"x{EVENT_ONLY}y", {"b": EVENT_ONLY}], "n": 3}, [EVENT_ONLY])
    assert EVENT_ONLY not in json.dumps(out) and out["n"] == 3


def test_secret_values_include_the_mcp_env_and_header_values():
    values = debugdump.secret_values(_config())
    assert EVENT_ONLY in values and HDR in values and PAT in values


# K3NOX-306: the review of PR #186 (ordinary values stay readable, export -> import round-trips, the class of each gap)
STD_B64 = "wJalr" + "XUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    "text",
    [
        "sort --key=2,2 data.csv",
        "gpg --recv-key 0x1234ABCD99EF",
        "psql --no-password dbname",
        "aws s3api get-object --bucket b --key docs/a.txt out.txt",
        "tool --sort-key name --primary-key id --cache-key build-1",
    ],
)
def test_scrub_text_leaves_ordinary_key_flags_alone(text):
    from k3code.redact import scrub_text

    assert scrub_text(text) == text


@pytest.mark.parametrize(
    "text, secret",
    [
        ("tool --key=" + PW, PW),
        ("tool --key " + FLAG_VALUE, FLAG_VALUE),
        ("tool --license-key " + PW, PW),
        ("tool --openai-api-key=" + PW, PW),
        ("tool --aws-secret-key " + PW, PW),
        ("tool --github-token " + PW, PW),
    ],
)
def test_scrub_text_still_hides_credential_flags(text, secret):
    from k3code.redact import scrub_text

    assert secret not in scrub_text(text)


def test_argv_flags_keep_their_name_and_only_a_token_value_is_hidden():
    out = redact({"args": ["--transport=streamableHttp", f"--config={ARGV_TOKEN}", "--no-token", "server.js"]})["args"]
    assert out == ["--transport=streamableHttp", f"--config={REDACTED}", "--no-token", "server.js"]


@pytest.mark.parametrize("query", [STD_B64, STD_B64.replace("/", "%2F")])
def test_a_base64_query_value_is_hidden_also_when_percent_encoded(query):
    out = redact({"url": f"https://mcp.example.com/mcp?foo={query}&limit=5"})["url"]
    assert "EXAMPLEKEY" not in out and "limit=5" in out


@pytest.mark.parametrize("arg", ["src/components/Button", "docs/api/v2", "@scope/pkg", "./relative/dir"])
def test_paths_with_slashes_are_not_base64(arg):
    assert redact({"args": ["serve", arg]})["args"] == ["serve", arg]


def test_a_token_in_a_url_dict_key_is_hidden():
    out = redact({"web": {f"https://api.example.com/x?token={QUERY_TOKEN}": "allow"}})
    assert QUERY_TOKEN not in json.dumps(out)


def test_ordinary_mcp_env_and_header_values_are_not_known_secrets():
    plain = ["127.0.0.1", "https://api.github.com", "Europe/Berlin", "en_US.UTF-8"]
    plain += ["application/json", "localhost:11434"]
    cfg = Settings(
        mcp={
            "servers": {
                "s": McpServerConfig(
                    command="srv",
                    env={f"V{i}": v for i, v in enumerate(plain[:-2])},
                    headers={"Accept": plain[-2], "X-Upstream": plain[-1]},
                )
            }
        }
    )
    values = debugdump.secret_values(cfg)
    assert not set(plain) & set(values)


def test_secret_named_and_scheme_values_are_known_secrets(monkeypatch):
    basic = "dXNlcjpw" + "YXNzd29yZA=="
    monkeypatch.setenv("MEM_LLAT", EVENT_ONLY)
    cfg = Settings(
        mem0={"url": "https://mem.example.com", "api_key_env": "MEM_LLAT"},
        mcp={
            "servers": {
                "s": McpServerConfig(
                    url="https://mcp.example.com/mcp",
                    env={"DB_PASSWORD": "a.b.c.d.e"},
                    headers={"Authorization": "Basic " + basic},
                )
            }
        },
    )
    values = debugdump.secret_values(cfg)
    assert {EVENT_ONLY, "a.b.c.d.e", basic} <= set(values)


def test_a_bearer_env_token_from_the_env_file_is_a_known_secret(tmp_path, monkeypatch):
    from k3code.setup.state import env_file_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    env_file_path().parent.mkdir(parents=True)
    env_file_path().write_text(f"HASS_LLAT={FLAG_VALUE}\n")
    server = McpServerConfig(url="https://ha.example.com/api/mcp", bearer_env="HASS_LLAT")
    cfg = Settings(mcp={"servers": {"ha": server}})
    assert FLAG_VALUE in debugdump.secret_values(cfg)


def _write_user_config(data: dict) -> None:
    import yaml

    from k3code.paths import user_config_path

    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text(yaml.safe_dump(data))


def test_export_then_import_round_trips_ordinary_settings(tmp_path):
    from k3code import bundle
    from k3code.gateway.sessions import SessionStore

    args = ["--model", "claude-3-5-sonnet-20241022", "--max-old-space-size=4096", "--sort-key", "name"]
    provider = {"name": "p", "kind": "openai", "base_url": "http://127.0.0.1:4000/v1", "api_key_env": "K3_X_KEY"}
    server = {"command": "srv", "args": [*args, "--pat", PAT, "--port", "9000"], "env": {"HOST": "127.0.0.1"}}
    _write_user_config({"providers": [provider], "mcp": {"servers": {"s": server}}})
    out = tmp_path / "x.k3bundle"
    bundle.write_bundle(out, store=SessionStore(tmp_path / "s.db"), cwd=tmp_path, session_ids=[])
    assert PAT not in json.dumps(bundle.read_bundle(out).settings)

    merged = bundle.merge_settings({}, bundle.read_bundle(out).settings["user"])
    assert merged["providers"] == [provider]
    # the token stays a marker in its place, so `--port 9000` is still `--port 9000`
    assert merged["mcp"]["servers"]["s"]["args"] == [*args, "--pat", REDACTED, "--port", "9000"]


def test_export_drops_a_known_secret_before_the_shape_pass(tmp_path, monkeypatch):
    from k3code import bundle
    from k3code.gateway.sessions import SessionStore

    tail = "Tail" + "Part77zz"
    monkeypatch.setenv("K3_TEST_API_TOKEN", "Tok" + "PartOne99," + tail)
    _write_user_config({"hooks": {"pre": "export API_TOKEN=" + "Tok" + "PartOne99," + tail}})
    store = SessionStore(tmp_path / "s.db")
    sess = store.create(title="t")
    sess.messages = [{"role": "tool", "content": "export API_TOKEN=" + "Tok" + "PartOne99," + tail}]
    store.save(sess)
    out = tmp_path / "x.k3bundle"
    bundle.write_bundle(out, store=store, cwd=tmp_path, session_ids=[sess.session_id])
    with tarfile.open(out) as tar:
        blob = "\n".join(tar.extractfile(n).read().decode() for n in tar.getnames())
    assert tail not in blob


# K3NOX-363: a glob value is a permission pattern, not a credential; `--clientsecret` is a credential flag again

#: Deny rules that carry no secret but put a glob where a credential would sit (after `--pat`, in `?sig=`, `u:*@`).
GLOB_DENY_RULES = [
    "* --pat *",
    "* --bearer *",
    "gh * --pat=*",
    "curl *?sig=*",
    "curl *&signature=?*",
    "* --bearer ** *",
    "curl https://*:*@*",
    "curl *://*:*@*",
]


def test_glob_values_in_permission_keys_stay_patterns():
    # two of them must not collapse into one `... <redacted>` key either
    perms = {"bash": dict.fromkeys(GLOB_DENY_RULES, "deny")}
    assert redact({"permissions": perms}) == {"permissions": perms}


@pytest.mark.parametrize("rule", GLOB_DENY_RULES)
def test_a_deny_rule_survives_export_and_trusted_import(rule, tmp_path):
    from k3code import bundle
    from k3code.gateway.sessions import SessionStore
    from k3code.paths import user_config_path
    from k3code.permissions.rules import evaluate
    from k3code.permissions.state import permissions_from_text

    _write_user_config({"permissions": {"bash": {rule: "deny"}}})
    out = tmp_path / "x.k3bundle"
    bundle.write_bundle(out, store=SessionStore(tmp_path / "s.db"), cwd=tmp_path, session_ids=[])
    user_config_path().unlink()  # a new device: only the bundle's rules are there
    store = SessionStore(tmp_path / "t.db")
    bundle.apply_bundle(bundle.read_bundle(out), store=store, cwd=tmp_path, sessions=False, trust_bundle=True)
    rules, _ = permissions_from_text(user_config_path().read_text())
    command = rule.replace("*", "x" + "abc").replace("?", "q")
    assert evaluate("bash", command, rules).action == "deny"


@pytest.mark.parametrize(
    "rule, secret",
    [
        ("x --pat " + PW, PW),
        ("x --bearer=" + PW, PW),
        ("x --pat *" + PW, PW),
        ("x --pat " + PW + "?", PW),
        ("curl https://h.example.com/a?sig=" + SIG + "*", SIG),
        ("curl https://u:" + URL_PW + "@h.example.com/*", URL_PW),
        ("curl https://u:*" + URL_PW + "@h.example.com/*", URL_PW),
    ],
)
def test_a_value_that_is_not_only_glob_characters_is_still_hidden(rule, secret):
    from k3code.redact import scrub_text

    assert secret not in scrub_text(rule)
    assert secret not in json.dumps(redact({"permissions": {"bash": {rule: "allow"}}}))


@pytest.mark.parametrize(
    "flag",
    ["--clientsecret", "--clientSecret", "--CLIENTSECRET", "-clientsecret", "--oauth-clientsecret", "--client-secret"],
)
def test_client_secret_flags_hide_their_value(flag):
    from k3code.redact import scrub_text

    assert PW not in scrub_text(f"tool {flag} {PW}")
    assert PW not in scrub_text(f"tool {flag}={PW}")
    assert redact({"args": [flag, PW]})["args"] == [flag, REDACTED]
