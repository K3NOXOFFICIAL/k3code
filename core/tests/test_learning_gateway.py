"""M5 through the gateway (scripted provider): approvals across sessions → proposal → accept/dismiss."""

from __future__ import annotations

import json

from k3code.confio import read_yaml
from k3code.learning.decisions import DecisionLog
from test_permissions_gateway import bash, call, make_server, run_turn


def shown(server, kind=None):
    out = []
    for line in server._frames:
        f = json.loads(line)
        is_card = f.get("method") == "event" and f["params"]["type"] == "proposal.show"
        if is_card and (kind is None or f["params"]["payload"].get("kind") == kind):
            out.append(f["params"]["payload"])
    return out


async def approve_in_new_session(tmp_path, monkeypatch, cmd, choice="once"):
    server, _ = make_server(tmp_path, [bash(cmd), "ok"], monkeypatch)
    sess = await call(server, "session.create", {"cwd": str(tmp_path)})
    await run_turn(server, "go", [{"choice": choice}])
    await server.learning.drain()
    return server, sess["session_id"]


async def test_three_approvals_across_sessions_propose_rule_and_accept_writes_config(tmp_path, monkeypatch):
    for i in range(3):
        server, sid = await approve_in_new_session(tmp_path, monkeypatch, f"echo run-{i}")
        if i < 2:
            assert not shown(server, "permission_rule")
    cards = shown(server, "permission_rule")
    assert len(cards) == 1 and "echo *" in cards[0]["text"] and "allow rule" in cards[0]["text"]
    out = await call(server, "command.dispatch", {"name": "permissions", "arg": "suggest", "session_id": sid})
    assert "echo *" in out["output"] and "project config" in out["output"]
    arg = f"accept {cards[0]['id']}"
    res = await call(server, "command.dispatch", {"name": "proposals", "arg": arg, "session_id": sid})
    assert "written" in res["output"]
    assert read_yaml(tmp_path / ".k3code" / "config.yaml")["permissions"]["bash"]["echo *"] == "allow"
    # a fourth session no longer asks
    server4, _ = make_server(tmp_path, [bash("echo run-9"), "ok"], monkeypatch)
    await call(server4, "session.create", {"cwd": str(tmp_path)})
    assert await run_turn(server4, "go", []) == []
    rows = DecisionLog(tmp_path / "home").query()
    assert [r["kind"] for r in rows].count("approval") == 3 and rows[-1]["kind"] == "proposal"


async def test_dismissed_rule_proposal_never_returns(tmp_path, monkeypatch):
    for i in range(3):
        server, sid = await approve_in_new_session(tmp_path, monkeypatch, f"echo run-{i}")
    (card,) = shown(server, "permission_rule")
    await call(server, "command.dispatch", {"name": "proposals", "arg": f"dismiss {card['id']}", "session_id": sid})
    for i in range(3, 6):
        server, sid = await approve_in_new_session(tmp_path, monkeypatch, f"echo run-{i}")
        assert not shown(server, "permission_rule")
    await call(server, "command.dispatch", {"name": "permissions", "arg": "suggest", "session_id": sid})
    assert not shown(server, "permission_rule")
    assert [r["choice"] for r in DecisionLog(tmp_path / "home").query("proposal")] == ["dismiss"]


async def test_other_decisions_are_logged(tmp_path, monkeypatch):
    server, sid = await approve_in_new_session(tmp_path, monkeypatch, "echo x", choice="deny")
    await call(server, "command.dispatch", {"name": "model", "arg": "fast-one too slow", "session_id": sid})
    await call(server, "command.dispatch", {"name": "scope", "arg": "small", "session_id": sid})
    await call(server, "command.dispatch", {"name": "config", "arg": "set max_turns 9", "session_id": sid})
    await call(server, "command.dispatch", {"name": "config", "arg": "set max_turns 8", "session_id": sid})
    await call(server, "command.dispatch", {"name": "config", "arg": "rollback", "session_id": sid})
    log = DecisionLog(tmp_path / "home")
    assert log.query("approval")[0]["choice"] == "deny"
    ms = log.query("model_switch")[0]
    assert ms["detail"]["to"] == "fast-one" and ms["detail"]["reason"] == "too slow"
    assert log.query("scope")[0]["choice"] == "small"
    assert log.query("config")[0]["subject"] == "max_turns" and log.query("undo")


async def test_commands_optimizer_selfimprove_and_project_prep(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module x\n")
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    sess = await call(server, "session.create", {"cwd": str(tmp_path)})
    await server.learning.drain()
    sid = sess["session_id"]
    assert (tmp_path / ".k3code" / "project.json").is_file() and not (tmp_path / "K3CODE.md").exists()
    kinds = {c["kind"] for c in shown(server)}
    assert kinds == {"project_setup"}
    st = await call(server, "command.dispatch", {"name": "optimizer", "arg": "status", "session_id": sid})
    assert "off" in st["output"]
    si = await call(server, "command.dispatch", {"name": "self-improve", "arg": "faster startup", "session_id": sid})
    assert list((tmp_path / ".k3code" / "self-improve").glob("*.md")) and "draft" in si["output"]
