"""M1 gateway tests: protocol framing, approvals, dispatch, fake provider, cooldown.

Covers the M1 acceptance checks: JSON-RPC framing round-trips, unknown
methods return -32601, approval request/response resolves through the
server→client channel, command.dispatch + slash.exec reach the registry,
the scripted fake provider replays text/tool/usage/error, and network
failures arm the configurable cooldown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from k3code.commands.builtin import build_registry
from k3code.config import ProviderEntry, Settings
from k3code.gateway.protocol import (
    METHOD_NOT_FOUND,
    decode_frame,
    encode_error,
    encode_response,
    next_request_id,
)
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.providers import make_providers
from k3code.providers.base import ProviderError
from k3code.providers.fake import FakeProvider
from k3code.providers.types import Message
from k3code.router.classifier import FailoverReason
from k3code.router.cooldown import CooldownStore, network_cooldown_seconds

# ── helpers ─────────────────────────────────────────────────────────


def make_server(**overrides) -> GatewayServer:
    """GatewayServer with an in-memory-ish sqlite store (temp dir) and no providers."""
    tmp = tempfile.mkdtemp(prefix="k3gateway-test-")
    store = SessionStore(Path(tmp) / "sessions.db")
    config = overrides.pop(
        "config",
        Settings(
            providers=[ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE")],
        ),
    )
    server = GatewayServer(config=config, store=store, **overrides)
    written: list[str] = []
    server._write = written.append  # type: ignore[method-assign]  # capture frames
    server._frames = written  # type: ignore[attr-defined]
    return server


def frames_of(server: GatewayServer) -> list[dict]:
    return [json.loads(line) for line in server._frames]  # type: ignore[attr-defined]


async def rpc(server: GatewayServer, method: str, params: dict | None = None, req_id: int = 1) -> dict:
    """Drive one request line through the server; return the parsed response frame."""
    n = len(server._frames)  # type: ignore[attr-defined]
    await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}))
    out = frames_of(server)[n:]
    assert len(out) == 1, f"expected one response frame, got {out}"
    return out[0]


# ── 1. protocol framing ─────────────────────────────────────────────


def test_decode_round_trip():
    line = encode_response(3, {"ok": True})
    obj, err = decode_frame(line)
    assert err is None and obj is not None
    assert obj["id"] == 3 and obj["result"] == {"ok": True}


def test_decode_garbage_is_parse_error():
    obj, err = decode_frame("{not json")
    assert obj is None and err is not None and err.startswith("parse error")


def test_decode_non_object_is_invalid_request():
    obj, err = decode_frame("[1,2]")
    assert obj is None and "invalid request" in err


def test_encode_error_shape():
    frame = json.loads(encode_error(7, METHOD_NOT_FOUND, "Method not found: x"))
    assert frame == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "Method not found: x"}}


def test_server_request_ids_unique():
    ids = {next_request_id() for _ in range(50)}
    assert len(ids) == 50


# ── 2. unknown method → -32601 ──────────────────────────────────────


async def test_unknown_method_returns_32601():
    server = make_server()
    frame = await rpc(server, "bogus.method", {}, req_id=9)
    assert frame["id"] == 9
    assert frame["error"]["code"] == METHOD_NOT_FOUND == -32601
    await server.close()


async def test_garbage_line_emits_parse_error_frame():
    server = make_server()
    await server._handle_line("{{{")
    out = frames_of(server)
    assert len(out) == 1 and out[0]["error"]["code"] == -32700
    await server.close()


# ── 3. session.create + prompt.submit lifecycle ─────────────────────


async def test_session_create_fields():
    server = make_server()
    frame = await rpc(server, "session.create", {"model": "m", "cwd": "/tmp"})
    result = frame["result"]
    assert result["session_id"] and result["info"]["model"] == "m"
    assert result["info"]["stored_session_id"] == result["session_id"]
    await server.close()


async def test_session_list_rows_carry_the_session_cwd():
    """The agent view lists only earlier sessions of the current project, so each row says where it ran."""
    server = make_server()
    sid = (await rpc(server, "session.create", {"model": "m", "cwd": "/tmp/proj"}))["result"]["session_id"]
    unknown = server.store.create(cwd="").session_id
    rows = {r["id"]: r for r in (await rpc(server, "session.list", req_id=2))["result"]["sessions"]}
    assert rows[sid]["cwd"] == "/tmp/proj"
    assert rows[unknown]["cwd"] is None
    await server.close()


async def test_session_cwd_is_normalised_for_equality(tmp_path):
    """The agent view filters earlier sessions by strict cwd equality: a symlink or trailing slash must not split one
    project into two."""
    real = tmp_path / "proj"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    server = make_server()
    created = (await rpc(server, "session.create", {"model": "m", "cwd": f"{link}/"}))["result"]
    assert created["info"]["cwd"] == str(real.resolve())
    old = server.store.create(cwd=f"{real}/").session_id  # stored before normalisation
    rows = {r["id"]: r for r in (await rpc(server, "session.list", req_id=2))["result"]["sessions"]}
    assert rows[created["session_id"]]["cwd"] == str(real.resolve())
    assert rows[old]["cwd"] == str(real.resolve())
    await server.close()


def _stored(server: GatewayServer, cwd: str, *, updated_at: float, messages: int = 1, origin: str = "") -> str:
    """A stored session with ``messages`` user turns and a fixed ``updated_at`` (list order is by that column)."""
    s = server.store.create(cwd=cwd)
    s.messages = [{"role": "user", "content": f"q{i}"} for i in range(messages)]
    if origin:
        s.meta["origin"] = origin
    server.store.save(s)
    server.store._db.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (updated_at, s.session_id))
    server.store._db.commit()
    return s.session_id


async def test_session_list_cwd_returns_only_that_projects_sessions(tmp_path):
    """The agent view asks for the current project's earlier sessions: another project's, an automation run's and an
    empty one's stay out, and an old row spelled through a symlink or with a trailing slash still counts."""
    real = tmp_path / "proj"
    real.mkdir()
    (tmp_path / "other").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    server = make_server()
    plain = _stored(server, str(real), updated_at=10)
    via_link = _stored(server, f"{link}/", updated_at=9)
    slash = _stored(server, f"{real}/", updated_at=8)
    _stored(server, str(tmp_path / "other"), updated_at=11)
    _stored(server, str(real), updated_at=12, origin="automation")
    _stored(server, str(real), updated_at=13, messages=0)
    res = (await rpc(server, "session.list", {"cwd": f"{link}/", "limit": 50}))["result"]["sessions"]
    assert [r["id"] for r in res] == [plain, via_link, slash]
    assert {r["cwd"] for r in res} == {str(real.resolve())}
    await server.close()


async def test_session_list_cwd_applies_the_limit_after_filtering(tmp_path):
    """A busy daemon's newer sessions in other projects must not crowd this project's history out of the page."""
    (tmp_path / "proj").mkdir()
    (tmp_path / "busy").mkdir()
    server = make_server()
    mine = [_stored(server, str(tmp_path / "proj"), updated_at=t) for t in (2, 1, 0.5)]
    for t in range(300):  # more than one scan page, all newer than mine
        _stored(server, str(tmp_path / "busy"), updated_at=100 + t)
    res = (await rpc(server, "session.list", {"cwd": str(tmp_path / "proj"), "limit": 2}))["result"]["sessions"]
    assert [r["id"] for r in res] == mine[:2]
    await server.close()


async def test_session_list_cwd_scan_does_not_parse_every_transcript(tmp_path, monkeypatch):
    """The scan ran on the daemon's event loop parsing up to 2000 full transcripts (8 s on a big store), stalling
    every other client: only the matches' transcripts may be read into Python."""
    from k3code.gateway import server as server_mod

    (tmp_path / "proj").mkdir()
    server = make_server()
    blob = json.dumps([{"role": "user", "content": "x" * 20_000}])
    rows = []
    for i in range(600):
        cwd = str(tmp_path / "proj") if i in (0, 250, 599) else f"/elsewhere/{i % 5}"
        rows.append((f"s{i:04d}", cwd, blob, "{}", float(i)))
    rows.append(("empty", str(tmp_path / "proj"), "[]", "{}", 1000.0))
    rows.append(("cron", str(tmp_path / "proj"), blob, '{"origin": "automation"}', 1001.0))
    server.store._db.executemany(
        "INSERT INTO sessions (session_id, cwd, messages, meta, updated_at) VALUES (?,?,?,?,?)", rows
    )
    server.store._db.commit()
    real_loads = json.loads
    big_parses = 0

    def counting_loads(s, *a, **kw):
        nonlocal big_parses
        if isinstance(s, str) and len(s) > 10_000:
            big_parses += 1
        return real_loads(s, *a, **kw)

    monkeypatch.setattr(json, "loads", counting_loads)
    got = server_mod._project_sessions(server, str((tmp_path / "proj").resolve()), 50)
    monkeypatch.setattr(json, "loads", real_loads)
    assert [s.session_id for s in got] == ["s0599", "s0250", "s0000"]
    assert big_parses == 3  # the three matches, not the 600 rows scanned
    res = (await rpc(server, "session.list", {"cwd": str(tmp_path / "proj"), "limit": 50}))["result"]["sessions"]
    assert [r["id"] for r in res] == ["s0599", "s0250", "s0000"]
    await server.close()


async def test_session_list_without_cwd_is_unchanged(tmp_path):
    """Other callers (the session switcher) still get every project, automation runs and empty sessions included."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    server = make_server()
    ids = {
        _stored(server, str(tmp_path / "a"), updated_at=4),
        _stored(server, str(tmp_path / "b"), updated_at=3),
        _stored(server, str(tmp_path / "a"), updated_at=2, origin="automation"),
        _stored(server, str(tmp_path / "a"), updated_at=1, messages=0),
    }
    res = (await rpc(server, "session.list", {"limit": 50}))["result"]["sessions"]
    assert {r["id"] for r in res} == ids
    await server.close()


async def test_resumed_session_reports_its_cwd_normalised(tmp_path, monkeypatch):
    """A session stored as ``<symlink>/`` (before normalisation existed) reports the real path once resumed, so the
    agent view's project filter matches it."""
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    real = tmp_path / "proj"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    server = make_server()
    stored = server.store.create(model="m", cwd=f"{link}/")
    for req_id, method in ((7, "session.resume"), (8, "session.activate")):
        line = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": {"session_id": stored.session_id}}
        await server._handle_line(json.dumps(line))  # resume also emits progress events: pick our response by id
        res = next(f for f in frames_of(server) if f.get("id") == req_id)
        assert res["result"]["info"]["cwd"] == str(real.resolve()), method
    await server.close()


async def test_prompt_submit_without_session_is_invalid_params():
    server = make_server()
    frame = await rpc(server, "prompt.submit", {"text": "hi"})
    assert frame["error"]["code"] == -32602
    await server.close()


# ── 4. approval round-trip through the server→client channel ────────


async def test_approval_round_trip_allows_tool():
    server = make_server()
    await rpc(server, "session.create", {})

    answered: dict = {}

    async def client() -> None:
        # Wait for the approval request frame, then answer it.
        for _ in range(200):
            for line in list(server._frames):  # type: ignore[attr-defined]
                frame = json.loads(line)
                if frame.get("method") == "approval" and frame.get("id"):
                    await server._handle_line(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": frame["id"],
                                "result": {"choice": "once", "request_id": frame["id"]},
                            }
                        )
                    )
                    answered["id"] = frame["id"]
                    return
            await asyncio.sleep(0.01)
        raise AssertionError("no approval request frame emitted")

    approve = await server._approval_callback_for(server.session)
    _, allowed = await asyncio.gather(client(), approve("bash", {"command": "echo hi"}))
    assert allowed  # gather order: (client None, ApprovalResult)
    assert answered["id"].startswith("approval-")
    await server.close()


async def test_approval_deny_blocks_tool():
    server = make_server()
    await rpc(server, "session.create", {})

    async def client() -> None:
        for _ in range(200):
            for line in list(server._frames):  # type: ignore[attr-defined]
                frame = json.loads(line)
                if frame.get("method") == "approval" and frame.get("id"):
                    await server._handle_line(
                        json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {"choice": "deny"}})
                    )
                    return
            await asyncio.sleep(0.01)
        raise AssertionError("no approval request frame emitted")

    approve = await server._approval_callback_for(server.session)
    _, allowed = await asyncio.gather(client(), approve("bash", {"command": "rm -rf /"}))
    assert not allowed
    await server.close()


# ── 5. command.dispatch + slash.exec ────────────────────────────────


async def test_registry_names_available():
    registry = build_registry()
    for name in ("model", "effort", "clear", "compact", "rename", "resume", "stop", "exit", "help"):
        assert name in registry.names(), name


async def test_command_dispatch_help():
    server = make_server()
    frame = await rpc(server, "command.dispatch", {"name": "help"})
    assert frame["result"]["type"] in ("message", "commands")
    payload = json.dumps(frame["result"])
    assert "/help" in payload or "help" in payload
    await server.close()


async def test_command_dispatch_unknown_is_message():
    server = make_server()
    frame = await rpc(server, "command.dispatch", {"name": "nope-not-a-command"})
    assert frame["result"]["type"] == "message"
    assert "Unknown command" in frame["result"]["message"]
    await server.close()


async def test_slash_exec_accepts_command_without_slash():
    server = make_server()
    frame = await rpc(server, "slash.exec", {"command": "help"})
    assert "Commands:" in frame["result"]["output"]
    await server.close()


async def test_slash_exec_routes_to_command():
    server = make_server()
    frame = await rpc(server, "slash.exec", {"command": "/help"})
    assert "error" not in frame
    assert frame["result"]["type"] in ("message", "commands")
    await server.close()


# ── 6. fake provider ────────────────────────────────────────────────


def test_fake_provider_replays_text_tool_usage():
    provider = FakeProvider(
        name="fake",
        steps=[
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
            {"type": "tool_call", "id": "c1", "name": "bash", "arguments": {"command": "echo hi"}},
            {"type": "usage", "prompt_tokens": 5, "completion_tokens": 7},
        ],
    )

    async def go():
        seen: list[str] = []
        final = None
        async for ev in provider.stream([Message(role="user", content="x")], [], "m"):
            seen.append(ev.type)
            if ev.type == "done":
                final = ev.message
        return seen, final

    seen, final = asyncio.get_event_loop().run_until_complete(go()) if False else _run(go())
    assert seen[0] == "text_delta" and seen[-1] == "done"
    assert final is not None and final.content == "hello world"
    assert final.tool_calls and final.tool_calls[0].name == "bash"
    assert final.usage is not None and (final.usage.prompt_tokens, final.usage.completion_tokens) == (5, 7)


def _run(coro):
    import asyncio as _aio

    return _aio.run(coro)


def test_fake_provider_error_aborts_with_provider_error():
    provider = FakeProvider(
        name="fake",
        steps=[{"type": "error", "status_code": 503, "message": "down", "headers": {"retry-after": "2"}}],
    )

    async def go():
        async for _ in provider.stream([Message(role="user", content="x")], [], "m"):
            pass

    with pytest.raises(ProviderError) as excinfo:
        _run(go())
    assert excinfo.value.status_code == 503
    assert excinfo.value.headers.get("retry-after") == "2"


def test_make_providers_honors_fake_env(tmp_path, monkeypatch):
    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"type": "text", "text": "canned"}]))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    entries = [ProviderEntry(name="p1", kind="openai", base_url="http://x", api_key_env="NOPE")]
    providers = make_providers(entries)
    assert len(providers) == 1 and isinstance(providers[0], FakeProvider)
    assert providers[0].steps == [{"type": "text", "text": "canned"}]


def test_make_providers_fake_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError):
        make_providers([ProviderEntry(name="p", kind="openai", base_url="http://x", api_key_env="NOPE")])


# ── 7. network cooldown ─────────────────────────────────────────────


def test_network_failure_arms_cooldown_by_default():
    store = CooldownStore()
    armed = store.arm(FailoverReason.network, provider="p", model="m", base_url="http://x")
    assert armed == 60  # flat default
    assert store.in_cooldown(provider="p", model="m", base_url="http://x")


def test_network_cooldown_env_zero_disables(monkeypatch):
    monkeypatch.setenv("K3CODE_NETWORK_COOLDOWN_SECONDS", "0")
    assert network_cooldown_seconds() == 0.0
    store = CooldownStore()
    assert store.arm(FailoverReason.network, provider="p", model="m") is None
    assert not store.in_cooldown(provider="p", model="m")


def test_network_cooldown_env_custom(monkeypatch):
    monkeypatch.setenv("K3CODE_NETWORK_COOLDOWN_SECONDS", "5")
    assert network_cooldown_seconds() == 5.0
    store = CooldownStore()
    assert store.arm(FailoverReason.network, provider="p", model="m") == 5


def test_network_cooldown_expires(monkeypatch):
    monkeypatch.setenv("K3CODE_NETWORK_COOLDOWN_SECONDS", "1")
    store = CooldownStore()
    store.arm(FailoverReason.network, provider="p", model="m", now=100.0)
    assert store.in_cooldown(provider="p", model="m", now=100.5)
    assert not store.in_cooldown(provider="p", model="m", now=101.5)


def test_non_cooldown_reasons_do_not_arm():
    store = CooldownStore()
    assert store.arm(FailoverReason.auth, provider="p", model="m") is None
    assert not store.in_cooldown(provider="p", model="m")


def test_env_var_cleanup(monkeypatch):
    monkeypatch.delenv("K3CODE_NETWORK_COOLDOWN_SECONDS", raising=False)
    monkeypatch.delenv("K3CODE_FAKE_PROVIDER", raising=False)
    assert os.environ.get("K3CODE_FAKE_PROVIDER") is None


async def test_config_set_model_switches_session_model():
    """TUI /model <key> and the picker send config.set key=model; it must not be 'unsupported'."""
    server = make_server(
        config=Settings(
            providers=[
                ProviderEntry(
                    name="t",
                    kind="openai",
                    base_url="http://t",
                    api_key_env="NOPE",
                    models={"default": "a", "cheap": "b"},
                )
            ]
        )
    )
    sid = (await rpc(server, "session.create", {"model": "default", "cwd": "/tmp"}))["result"]["session_id"]
    n = len(server._frames)  # type: ignore[attr-defined]
    line = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "config.set",
        "params": {"key": "model", "session_id": sid, "value": "cheap --provider t --session"},
    }
    await server._handle_line(json.dumps(line))
    ok = next(f for f in frames_of(server)[n:] if f.get("id") == 2)
    assert ok["result"]["value"] == "cheap"
    assert server._session_for(sid).stored.model == "cheap"
    bad = await rpc(server, "config.set", {"key": "model", "session_id": sid, "value": "nope"}, req_id=3)
    assert "error" in bad
    await server.close()


async def test_a_stopped_turn_is_persisted_and_the_user_prompt_survives(tmp_path, monkeypatch):
    """/stop or a shutdown cancels the turn task; the CancelledError used to skip the persist block entirely,
    so the whole in-flight turn (prompt, tool calls, results) disappeared from the session store."""
    import json as _json

    from k3code.gateway.server import GatewayServer as _GS

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path.parent / f"{tmp_path.name}-k3home"))  # beside the project
    script = tmp_path / "s.json"
    script.write_text(_json.dumps([{"type": "tool_call", "name": "bash", "arguments": {"command": "echo one"}}]))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    srv = _GS(
        config=Settings(providers=[prov], default_model="default", permission_mode="yolo"),
        store=SessionStore(tmp_path / "s.db"),
    )
    srv._write = lambda s: None
    stored = srv.store.create(title="t", model="default", cwd=str(tmp_path))
    live = srv.live_for(stored)

    started = asyncio.Event()
    real = srv._checkpoint_turn

    async def hang_in_tool(*a, **k):
        started.set()
        await asyncio.sleep(60)

    import k3code.tools as tools_mod

    monkeypatch.setattr(tools_mod, "tool_bash", hang_in_tool, raising=False)
    task = asyncio.create_task(srv._run_turn(live, "do a long thing"))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = srv.store.get(live.session_id).messages
    assert any(m["role"] == "user" and m["content"] == "do a long thing" for m in saved), saved
    assert real is not None


async def test_tool_results_checkpoint_the_turn_before_it_ends(tmp_path, monkeypatch):
    """kill -9 / an OOM kill / a reboot mid-turn lost the whole turn: the store was written once, at the end."""
    import json as _json

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    script = tmp_path / "s.json"
    script.write_text(
        _json.dumps(
            [
                {"type": "tool_call", "name": "bash", "arguments": {"command": "echo one"}, "when": "turn_first"},
                {"type": "text", "text": "all done", "when": "turn_after_tool"},
            ]
        )
    )
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    srv = GatewayServer(
        config=Settings(providers=[prov], default_model="default", permission_mode="yolo"),
        store=SessionStore(tmp_path / "s.db"),
    )
    srv._write = lambda s: None
    srv.CHECKPOINT_EVERY_S = 0.0
    stored = srv.store.create(title="t", model="default", cwd=str(tmp_path))
    live = srv.live_for(stored)
    saved_sizes: list[int] = []
    real_save = srv.store.save
    srv.store.save = lambda s: (saved_sizes.append(len(s.messages)), real_save(s))[1]  # type: ignore[method-assign]
    status, text = await srv._run_turn(live, "run echo")
    assert status == "done", (text, live.last_error)
    final = len(live.stored.messages)
    assert any(0 < n < final for n in saved_sizes), (saved_sizes, final)  # a checkpoint landed before the end


async def test_idle_sessions_stop_their_netwatch_and_rearm_on_the_next_turn(tmp_path, monkeypatch):
    """Every live session kept a started NetWatch (two forever-tasks) until the daemon exited."""
    import json as _json

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    script = tmp_path / "s.json"
    script.write_text(_json.dumps([{"type": "text", "text": "ok"}]))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    srv = GatewayServer(
        config=Settings(providers=[prov], default_model="default", permission_mode="yolo"),
        store=SessionStore(tmp_path / "s.db"),
    )
    srv._write = lambda s: None
    stored = srv.store.create(title="t", model="default", cwd=str(tmp_path))
    live = srv.live_for(stored)
    await srv._run_turn(live, "hi")
    assert live.reliability is not None and live.reliability._started  # armed for the turn
    assert await srv.sweep_idle_reliability(now=live.idle_since + 10) == 0  # not idle long enough
    assert await srv.sweep_idle_reliability(now=live.idle_since + srv.IDLE_RELIABILITY_S + 1) == 1
    assert not live.reliability._started
    await srv._run_turn(live, "again")  # the next turn re-arms it
    assert live.reliability._started
    await srv.close()


async def test_alternating_model_keys_do_not_rebuild_the_provider_stack(tmp_path, monkeypatch):
    """Sessions on different model keys (a cron job with model: next to an interactive one) rebuilt providers, the
    cooldown store and every tier router on each alternating turn, closing none of the old ones."""
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    prov = ProviderEntry(
        name="t",
        kind="openai",
        base_url="http://t",
        api_key_env="NOPE",
        api_key="x",
        models={"default": "m", "alt": "m2"},
    )
    config = Settings(providers=[prov], default_model="default")
    srv = GatewayServer(config=config, store=SessionStore(tmp_path / "s.db"))
    srv._ensure_router("default")
    providers, cooldowns, main = srv.providers, srv.cooldowns, srv.router
    for _ in range(20):
        srv._ensure_router("alt")
        srv._ensure_router("default")
    assert srv.providers is providers and srv.cooldowns is cooldowns
    assert srv.router is main and srv._chain_key == "default"
    assert len(srv._router_cache) == 2
    # replacing the provider config (/config reload) does rebuild everything, once
    srv.config.providers = [prov]
    srv._chain_key = None
    srv._ensure_router("default")
    assert srv.providers is not providers and len(srv._router_cache) == 1
    await srv.close()


async def test_the_prompt_and_the_tool_call_are_on_disk_before_the_first_tool_runs(tmp_path, monkeypatch):
    """A kill -9 during the first long tool lost the whole turn, user prompt included: checkpoints only followed tool
    results. The assistant's tool call now triggers one too."""
    import json as _json

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    script = tmp_path / "s.json"
    script.write_text(_json.dumps([{"type": "tool_call", "name": "bash", "arguments": {"command": "sleep 30"}}]))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    db = tmp_path / "s.db"
    srv = GatewayServer(
        config=Settings(providers=[prov], default_model="default", permission_mode="yolo"), store=SessionStore(db)
    )
    srv._write = lambda s: None
    stored = srv.store.create(title="t", model="default", cwd=str(tmp_path))
    live = srv.live_for(stored)
    task = asyncio.create_task(srv._run_turn(live, "run the long thing"))
    try:
        await asyncio.sleep(1.5)  # the turn is inside `sleep 30`
        other = SessionStore(db)  # a separate connection: what a restarted daemon would read
        roles = [m["role"] for m in other.get(live.session_id).messages]
        assert "user" in roles and "assistant" in roles, roles
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_prompt_submitted_mid_turn_is_queued_and_runs_after_it(tmp_path, monkeypatch):
    """prompt.submit during a turn answered {"status": "queued"} and discarded the text: nothing ran, no error."""
    import json as _json

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    script = tmp_path / "s.json"
    script.write_text(_json.dumps([{"type": "text", "text": "done"}]))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    server = make_server(config=Settings(providers=[prov], default_model="default", permission_mode="yolo"))
    created = await rpc(server, "session.create", {"cwd": str(tmp_path)})
    live = server.session
    assert live is not None and created["result"]["session_id"] == live.session_id
    gate = asyncio.Event()
    real = server._run_turn_locked
    texts: list[str] = []

    async def gated(session, text):
        texts.append(text)
        if len(texts) == 1:
            await gate.wait()  # hold the first turn open
        return await real(session, text)

    server._run_turn_locked = gated  # type: ignore[method-assign]
    first = await rpc(server, "prompt.submit", {"text": "first"}, req_id=2)
    assert first["result"]["status"] == "streaming"
    for _ in range(100):
        if live.streaming or texts:
            break
        await asyncio.sleep(0.01)
    live.streaming = True  # the turn is running
    second = await rpc(server, "prompt.submit", {"text": "second"}, req_id=3)
    assert second["result"]["status"] == "queued" and live.pending_prompts == ["second"]
    live.streaming = False
    gate.set()
    await asyncio.wait_for(live.turn_task, 20)
    assert texts == ["first", "second"] and live.pending_prompts == []
    users = [m["content"] for m in live.stored.messages if m["role"] == "user"]
    assert users == ["first", "second"] or users[-2:] == ["first", "second"]
    await server.close()


async def test_session_resume_reports_a_running_turn(tmp_path, monkeypatch):
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE")
    server = make_server(config=Settings(providers=[prov]))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    stored = server.store.create(title="t", model="m", cwd=str(tmp_path))
    live = server.live_for(stored)
    live.streaming = True
    line = {"jsonrpc": "2.0", "id": 7, "method": "session.resume", "params": {"session_id": stored.session_id}}
    await server._handle_line(json.dumps(line))  # resume also emits progress events: pick our response by id
    res = next(f for f in frames_of(server) if f.get("id") == 7)
    assert res["result"]["running"] is True and "status" in res["result"]
    live.streaming = False
    await server.close()
