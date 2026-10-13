"""K3NOX-271 review of PR #186: secrets that still reach the /debug dump bundle. Each test fails on 89c3929.

Drop into core/tests/ next to test_debugdump_redaction.py. Fake values only, built at run time.
"""

from __future__ import annotations

import json
import tarfile

from k3code import debugdump
from k3code.config import McpServerConfig, ProviderEntry, Settings
from k3code.redact import redact
from test_gateway import make_server

BEARER = "Opaq" + "ueBearer" + "771x"  # an MCP header value of the form "Bearer <token>"
LLAT = "File" + "Env" + "Tok" + "en42ab"  # the token behind an MCP server's bearer_env
STD_B64 = "wJalr" + "XUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"  # standard base64 (with "/"), AWS-secret-like
QUOTED = 'quo"te' + "Secret12"  # a known secret containing a character JSON escapes


def _config() -> Settings:
    return Settings(
        providers=[
            ProviderEntry(name="p", kind="openai", base_url="http://127.0.0.1:9/v1", api_key_env="K3_X_KEY", models={})
        ],
        mcp={
            "servers": {
                "hdr": McpServerConfig(
                    url="https://mcp.example.com/mcp", headers={"Authorization": "Bearer " + BEARER}
                ),
                "llat": McpServerConfig(url="https://ha.example.com/api/mcp", bearer_env="HASS_LLAT"),
                "q": McpServerConfig(command="srv", env={"DB_PASS": QUOTED}),
            }
        },
    )


async def _blob(tmp_path, *events: dict) -> str:
    server = make_server(config=_config())
    for payload in events:
        server.emit("tool_result", payload)
    path = await debugdump.write_bundle(server, 50, home=tmp_path, probe=False)
    with tarfile.open(path) as tar:
        return "\n".join(tar.extractfile(n).read().decode() for n in tar.getnames())


async def test_the_token_of_a_bearer_header_is_a_known_secret_on_its_own(tmp_path):
    # secret_values() adds "Bearer <token>" as one string; the token alone in event text is not matched
    blob = await _blob(tmp_path, {"output": f"server said: invalid token {BEARER}"})
    assert BEARER not in blob


async def test_the_bearer_env_token_of_an_mcp_server_is_a_known_secret(tmp_path, monkeypatch):
    # the variable name has no KEY/TOKEN/SECRET in it, so only the bearer_env reference marks it as a secret
    monkeypatch.setenv("HASS_LLAT", LLAT)
    blob = await _blob(tmp_path, {"output": f"401 for credential {LLAT}"})
    assert LLAT not in blob


def test_a_lone_standard_base64_argument_is_token_shaped():
    # _TOKEN_CHARS has no "/", so ~40% of random 32+ char base64 secrets are not hidden in `args`
    out = redact({"args": ["serve", STD_B64]})["args"]
    assert STD_B64 not in out


async def test_a_known_secret_is_dropped_from_json_text_and_dict_keys(tmp_path):
    # drop_values() skips dict keys, and redact_text() sees the JSON-escaped form (quo\\"te...)
    blob = await _blob(tmp_path, {"output": json.dumps({"x": QUOTED})}, {QUOTED: "as a key"})
    assert QUOTED.split('"')[1] not in blob  # the part after the quote, whatever escaping surrounds it


async def test_a_known_secret_with_a_comma_is_dropped_whole(tmp_path, monkeypatch):
    # introduced by the PR: redact() runs before drop_values(), cuts `API_TOKEN=<redacted>,<tail>` at the comma,
    # and the exact-value drop then no longer finds the secret
    tail = "Tail" + "Part77zz"
    monkeypatch.setenv("K3_TEST_API_TOKEN", "Tok" + "PartOne99," + tail)
    blob = await _blob(tmp_path, {"output": "export API_TOKEN=" + "Tok" + "PartOne99," + tail})
    assert tail not in blob


def test_a_token_in_a_permission_rule_key_is_hidden():
    # an `always` approval of a wildcard-unsafe command stores the exact command as a dict key (permissions.bash)
    tok = "Opaq" + "ueRule" + "4242zz"
    cfg = Settings(permissions={"bash": {"npx some-mcp --api-token " + tok: "allow"}})
    assert tok not in json.dumps(debugdump.safe_config(cfg, debugdump.secret_values(cfg)))


def test_ordinary_mcp_env_values_do_not_become_secrets_everywhere():
    # introduced by the PR: every non-alphabetic MCP env value of 8+ chars is a "known secret", so a host or URL is
    # dropped from every other setting too (k3code export then imports `http://<redacted>:4000/v1`)
    cfg = Settings(
        providers=[
            ProviderEntry(
                name="p", kind="openai", base_url="http://127.0.0.1:4000/v1", api_key_env="K3_X_KEY", models={}
            )
        ],
        mcp={"servers": {"gh": McpServerConfig(command="srv", env={"HOST": "127.0.0.1"})}},
    )
    out = debugdump.safe_config(cfg, debugdump.secret_values(cfg))
    assert out["providers"][0]["base_url"] == "http://127.0.0.1:4000/v1"


def test_ordinary_mcp_args_stay_readable():
    # introduced by the PR: these are "token-shaped" or follow a "secret flag"; export -> import drops them
    args = ["--model", "claude-3-5-sonnet-20241022", "--max-old-space-size=4096", "--sort-key", "name"]
    assert redact({"args": args})["args"] == args
