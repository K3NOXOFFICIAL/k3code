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
