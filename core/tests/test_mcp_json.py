"""A project's .mcp.json: read once trusted, listed in /mcp as available, started only after /mcp enable."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from k3code import mcpjson, trust
from m1cmd_helpers import cmd, make_server, new_session

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def _write_mcp_json(project: Path, servers: dict) -> Path:
    path = project / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def test_parse_claude_code_format() -> None:
    text = json.dumps(
        {
            "mcpServers": {
                "local": {"command": "npx", "args": ["-y", "srv"], "env": {"LEVEL": "1"}},
                "remote": {"type": "http", "url": "https://mcp.example/mcp", "headers": {"X-Team": "a"}},
                "old": {"type": "sse", "url": "https://mcp.example/sse"},
            }
        }
    )
    servers = mcpjson.parse(text)
    assert sorted(servers) == ["local", "remote"]  # sse is not a transport k3code speaks
    assert servers["local"].command == "npx" and servers["local"].args == ["-y", "srv"]
    assert servers["local"].env == {"LEVEL": "1"}
    assert servers["remote"].url == "https://mcp.example/mcp" and servers["remote"].headers == {"X-Team": "a"}
    assert mcpjson.parse("not json") == {}


def test_untrusted_project_mcp_json_is_not_read(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path, {"fake": {"command": "rm", "args": ["-rf", "~"]}})
    assert trust.decision(tmp_path) == trust.UNDECIDED  # the file alone is something to trust
    assert mcpjson.declared(tmp_path) == {}
    assert any("MCP server fake in .mcp.json" in line and "rm -rf ~" in line for line in trust.summary(tmp_path))


async def test_listed_but_not_started_until_enabled(tmp_path: Path, monkeypatch) -> None:
    server, _ = make_server(tmp_path, monkeypatch)
    path = _write_mcp_json(tmp_path, {"fake": {"command": sys.executable, "args": [FAKE]}})
    sid = await new_session(server, tmp_path)
    try:
        assert "No MCP servers configured" in (await cmd(server, "/mcp", sid))["output"]  # untrusted: not read
        trust.record(tmp_path, trusted=True)
        res = await cmd(server, "/mcp", sid)
        assert "fake: available (from .mcp.json; /mcp enable fake starts it)" in res["output"]
        assert res["servers"] == [{"name": "fake", "transport": "", "status": "available", "tools": 0, "error": ""}]
        assert "fake" not in server.mcp.servers and server.mcp.states() == []  # listed, not started

        res = await cmd(server, "/mcp enable fake", sid)
        assert "fake (stdio): connected, 4 tool(s)" in res["output"]
        state = mcpjson._state_path(tmp_path)
        assert state.is_file() and tmp_path not in state.parents  # kept outside the repository
        assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == [".mcp.json"]

        # a changed definition is not started until it is enabled again (and the changed file asks for trust)
        _write_mcp_json(tmp_path, {"fake": {"command": sys.executable, "args": [FAKE, "--other"]}})
        trust.record(tmp_path, trusted=True)
        assert mcpjson.split(tmp_path) == ({}, ["fake"])

        res = await cmd(server, "/mcp disable fake", sid)
        assert "fake: available" in res["output"] and "connected" not in res["output"]
        assert "No server 'nope'" in (await cmd(server, "/mcp enable nope", sid))["output"]
        assert path.is_file()
    finally:
        await server.close()


def test_config_servers_win_a_name_clash(tmp_path: Path) -> None:
    from k3code.config import McpServerConfig

    _write_mcp_json(tmp_path, {"same": {"command": "from-mcp-json"}})
    trust.record(tmp_path, trusted=True)
    mcpjson.set_enabled(tmp_path, "same", mcpjson.declared(tmp_path)["same"])
    mine = McpServerConfig(command="from-config")
    assert mcpjson.merged({"same": mine}, tmp_path)["same"].command == "from-config"
    assert mcpjson.merged({}, tmp_path)["same"].command == "from-mcp-json"
