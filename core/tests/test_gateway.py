"""M1 gateway tests: protocol framing, approvals, dispatch, fake provider, cooldown.

Covers the M1 acceptance checks: JSON-RPC framing round-trips, unknown
methods return -32601, approval request/response resolves through the
server→client channel, command.dispatch + slash.exec reach the registry,
the scripted fake provider replays text/tool/usage/error, and network
failures arm the configurable cooldown.
"""

from __future__ import annotations

import asyncio
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
    assert allowed is True  # gather order: (client None, approve bool)
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
    assert allowed is False
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


async def test_slash_exec_passthrough_without_slash():
    server = make_server()
    frame = await rpc(server, "slash.exec", {"command": "plain text"})
    assert frame["result"] == {"type": "send", "text": "plain text"}
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
