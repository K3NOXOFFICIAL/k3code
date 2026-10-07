"""k3 panes integration: state reports, approvals through the Inbox, /bg --pane, /fork --pane, read-only attach."""

from __future__ import annotations

import asyncio
import json

import pytest

from fake_tuios import FakeTuios
from k3code.integrations.panes import (
    PaneLink,
    clean_summary,
    k3code_argv,
    map_state,
    open_pane_spec,
)
from m1cmd_helpers import cmd, frames_of, make_server, new_session, rpc


@pytest.fixture
def tuios():
    t = FakeTuios()
    yield t
    t.close()


def event(state: str, text: str = "") -> str:
    payload = {"kind": "status", "text": text, "state": state}
    return json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "status.update", "payload": payload}})


def approval_frame(req_id: str = "approval-7") -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "approval", "params": {
        "session_id": "s1", "request_id": "r", "command": "rm -rf build", "description": "delete",
        "choices": ["once", "session", "always", "deny"], "tool_name": "bash", "pattern": "bash(rm:*)"}})


# ── state mapping and no-op ──────────────────────────────────────────

def test_state_mapping():
    assert map_state("working") == "working"
    assert map_state("needs_input") == "needs_input"
    assert map_state("completed") == "done"
    assert map_state("failed") == "errored"
    assert map_state("idle") == "idle"
    assert map_state("new") is None


def test_no_env_is_a_noop():
    assert PaneLink.from_env(environ={}) is None
    assert PaneLink.from_env(environ={"TUIOS_SOCKET": "/x"}) is None
    assert PaneLink.from_env(environ={"TUIOS_PANE_ID": "p"}) is None


def test_missing_socket_never_raises():
    link = PaneLink.from_env(environ={"TUIOS_SOCKET": "/nonexistent/t.sock", "TUIOS_PANE_ID": "p"})
    assert link is not None
    link.on_server_line(event("working"))
    link.reporter.flush()  # swallowed: nothing to report to


def test_clean_summary_is_one_short_line():
    assert clean_summary("a\n\tb\x1b[0m  c") == "a b [0m c"
    long = clean_summary("x" * 400)
    assert len(long.encode()) <= 150 and long.endswith("...")


# ── reporter ─────────────────────────────────────────────────────────

def test_reports_every_change_once(tuios):
    link = PaneLink.from_env(environ=tuios.env())
    for st in ("idle", "working", "working", "needs_input", "working", "idle", "idle", "completed"):
        link.on_server_line(event(st))
    link.reporter.flush()
    got = tuios.verbs("set-agent-state")
    # a turn that ends (working -> idle) reads "done"; idle at rest stays idle; an unattended run is done too
    assert [g["state"] for g in got] == ["idle", "working", "needs_input", "working", "done", "idle", "done"]
    assert got[1] == {"session": "work", "window": "pane-1", "state": "working", "harness": "k3code"}


def test_failed_maps_to_errored(tuios):
    link = PaneLink.from_env(environ=tuios.env())
    link.on_server_line(event("failed", "boom"))
    link.reporter.flush()
    assert tuios.verbs("set-agent-state")[0]["state"] == "errored"
    assert tuios.verbs("set-agent-state")[0]["message"] == "boom"


def test_pane_token_is_presented_first(tuios):
    env = {**tuios.env(), "TUIOS_PANE_TOKEN": "tok"}
    link = PaneLink.from_env(environ=env)
    link.on_server_line(event("working"))
    link.reporter.flush()
    assert [r["verb"] for r in tuios.requests] == ["pane-grants", "set-agent-state"]
    assert tuios.requests[0]["params"] == {"pane_id": "pane-1", "pane_token": "tok"}


# ── Inbox approval round trip ────────────────────────────────────────

def test_approval_is_held_and_deny_resolves(tuios):
    tuios.answers["request-approval"] = lambda p: {"request_id": "h1", "decision": "deny", "message": "no thanks",
                                                   "reason": "answered"}
    injected: list[tuple[str, str, dict]] = []
    link = PaneLink.from_env(lambda *a: injected.append(a), environ=tuios.env())
    link.sync = True
    link.on_server_line(approval_frame())
    states = tuios.verbs("set-agent-state")
    assert states[0]["state"] == "needs_input" and states[0]["kind"] == "approval"
    held = tuios.verbs("request-approval")[0]
    assert held["window"] == "pane-1" and held["harness"] == "k3code" and held["summary"] == "rm -rf build"
    assert held["options"] == ["once", "always", "deny"]  # "session" has no Inbox key
    assert held["always_scope"] and held["tool"] == "bash"
    assert injected == [("approval-7", "approval", {"choice": "deny", "reason": "no thanks"})]


def test_no_answer_leaves_the_prompt_to_the_pane(tuios):
    tuios.answers["request-approval"] = lambda p: {"request_id": "", "decision": "", "reason": "viewed"}
    injected: list = []
    link = PaneLink.from_env(lambda *a: injected.append(a), environ=tuios.env())
    link.sync = True
    link.on_server_line(approval_frame())
    assert injected == []


def test_always_without_a_pattern_is_not_offered(tuios):
    link = PaneLink.from_env(lambda *a: None, environ=tuios.env())
    p = link.approval_params({"choices": ["once", "always", "deny"]}, "x")
    assert p["options"] == ["once", "deny"] and "always_scope" not in p


async def test_inbox_deny_resolves_the_gateway_approval(tuios, tmp_path, monkeypatch):
    """Whole path: gateway asks for approval -> tap holds it in the (fake) Inbox -> the Inbox says deny."""
    tuios.answers["request-approval"] = lambda p: {"request_id": "h1", "decision": "deny", "reason": "answered"}
    server, _ = make_server(tmp_path, monkeypatch)
    server.panes = PaneLink.from_env(server._pane_inject, environ=tuios.env())
    plain = server._write

    def tapped(line: str) -> None:
        plain(line)
        server.panes.on_server_line(line)

    server._write = tapped  # type: ignore[method-assign]
    sid = await new_session(server, tmp_path)
    approve = await server._approval_callback_for(server.live[sid])
    result = await asyncio.wait_for(approve("bash", {"command": "rm -rf build"}), 10)
    assert result.choice == "deny"
    assert tuios.verbs("request-approval")[0]["tool"] == "bash"
    cancels = [f for f in frames_of(server) if f.get("method") == "request.cancel"]
    assert cancels and cancels[0]["params"]["reason"].startswith("answered")


async def test_inbox_always_resolves_as_always(tuios, tmp_path, monkeypatch):
    tuios.answers["request-approval"] = lambda p: {"request_id": "h1", "decision": "always", "reason": "answered"}
    server, _ = make_server(tmp_path, monkeypatch)
    server.panes = PaneLink.from_env(server._pane_inject, environ=tuios.env())
    plain = server._write
    server._write = lambda line: (plain(line), server.panes.on_server_line(line))  # type: ignore[method-assign]
    sid = await new_session(server, tmp_path)
    approve = await server._approval_callback_for(server.live[sid])
    result = await asyncio.wait_for(approve("bash", {"command": "ls"}), 10)
    assert result.choice == "always"


def test_answering_in_the_pane_cancels_the_hold(tuios):
    import threading

    gate = threading.Event()
    tuios.answers["request-approval"] = lambda p: (gate.wait(3), {"decision": "once"})[1]
    injected: list = []
    link = PaneLink.from_env(lambda *a: injected.append(a), environ=tuios.env())
    link.on_server_line(approval_frame())
    tuios.wait_for("request-approval")
    link.on_client_line(json.dumps({"jsonrpc": "2.0", "id": "approval-7", "result": {"choice": "once"}}))
    gate.set()
    link.join()
    assert injected == []  # the pane's own answer won; the Inbox answer is dropped


# ── new panes ────────────────────────────────────────────────────────

async def test_bg_pane_sends_start_agent(tuios, tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, replies=["done"])
    server.panes = PaneLink.from_env(environ=tuios.env())
    plain = server._write
    server._write = lambda line: (plain(line), server.panes.on_server_line(line))  # type: ignore[method-assign]
    sid = await new_session(server, tmp_path)
    res = await cmd(server, "/bg --pane write a poem", sid)
    new_sid = res["session_id"]
    assert res["open_pane"]["session_id"] == new_sid and not res["open_pane"]["readonly"]
    # the result frame is what the tap reads
    await rpc(server, "slash.exec", {"command": "bg --pane again", "session_id": sid})
    started = tuios.wait_for("start-agent")
    assert started
    p = started[0]
    assert p["session"] == "work" and p["focus"] is False
    assert " attach " in p["agent"] and new_sid in p["agent"] and "--readonly" not in p["agent"]


async def test_bg_pane_outside_panes_is_a_plain_bg(tuios, tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, replies=["done"])
    assert server.panes is None
    sid = await new_session(server, tmp_path)
    res = await cmd(server, "/bg --pane write a poem", sid)
    assert res["session_id"] in server.live  # the background session started anyway
    assert tuios.verbs("start-agent") == []
    plain = await cmd(server, "/bg write a poem", sid)
    assert "open_pane" not in plain


async def test_fork_pane_sends_start_agent(tuios, tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    server.panes = PaneLink.from_env(environ=tuios.env())
    plain = server._write
    server._write = lambda line: (plain(line), server.panes.on_server_line(line))  # type: ignore[method-assign]
    sid = await new_session(server, tmp_path)
    res = await rpc(server, "slash.exec", {"command": "fork --pane", "session_id": sid})
    new_sid = res["result"]["session_id"]
    p = tuios.wait_for("start-agent")[0]
    assert new_sid in p["agent"]


def test_fanout_child_pane_is_read_only_tail(tuios):
    link = PaneLink.from_env(environ=tuios.env())
    link.on_server_line(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
        "type": "pane.open", "payload": {"subagent_id": "sa-1234", "name": "fan s1 parser"}}}))
    p = tuios.wait_for("start-agent")[0]
    assert " tail sa-1234" in p["agent"] and p["name"] == "fan s1 parser"


def test_argv_for_specs():
    assert k3code_argv(open_pane_spec("abc", readonly=True), {"PATH": ""})[-3:] == ["attach", "abc", "--readonly"]
    assert k3code_argv({"subagent_id": "sa-1"}, {"PATH": ""})[-2:] == ["tail", "sa-1"]
    assert k3code_argv({}, {}) is None


# ── read-only ────────────────────────────────────────────────────────

def test_readonly_refuses_changes(tuios):
    link = PaneLink.from_env(readonly=True, environ=tuios.env())
    err = link.on_client_line(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "prompt.submit", "params": {}}))
    assert err and json.loads(err)["error"]["message"] == "this pane is read-only"
    resume = json.dumps({"jsonrpc": "2.0", "id": 6, "method": "session.resume", "params": {}})
    assert link.on_client_line(resume) is None
    assert link.on_client_line(json.dumps({"jsonrpc": "2.0", "id": "approval-1", "result": {}})) == ""


async def test_fanout_panes_config_emits_pane_open(tmp_path, monkeypatch):
    from k3code.autonomy import autonomy_cfg

    server, _ = make_server(tmp_path, monkeypatch, autonomy={"fanout": {"panes": True}})
    assert autonomy_cfg(server.config)["fanout"]["panes"] is True
    server2, _ = make_server(tmp_path, monkeypatch)
    assert autonomy_cfg(server2.config)["fanout"]["panes"] is False


