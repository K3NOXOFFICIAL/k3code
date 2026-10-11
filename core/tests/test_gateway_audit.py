"""Gateway audit fixes: shared-config isolation, persistence robustness, malformed frames, session edges."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from k3code import trust
from k3code.gateway.protocol import decode_frame
from m1cmd_helpers import cmd, frames_of, make_server, new_session, rpc, submit_and_wait


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Home, config and project dirs all under tmp_path; nothing reaches the real home."""
    srv = tmp_path / "srv"
    srv.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("K3CODE_PROJECT_DIR", str(srv))
    for k in ("K3CODE_PERMISSION_MODE", "K3CODE_MAX_TURNS"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _trusted_project(root: Path, name: str, cfg: dict) -> Path:
    proj = root / name
    (proj / ".k3code").mkdir(parents=True)
    (proj / ".k3code" / "config.yaml").write_text(yaml.safe_dump(cfg))
    assert trust.record(proj, trusted=True)
    return proj


# ── H1: a session's project overlay stays out of the shared server config ─────────────────────────────────────


async def test_project_overlay_does_not_leak_into_server_config(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    x = _trusted_project(env, "x", {"permission_mode": "yolo", "max_turns": 3})
    y = env / "y"
    y.mkdir()
    sid = await new_session(server, x)
    res = await cmd(server, "/autocompact 60%", sid)  # a user-level write, then apply_file_config(session cwd)
    assert "Saved" in res["output"], res
    assert server.config.permission_mode == "ask" and server.config.max_turns == 0
    sid_y = await new_session(server, y)
    assert server.live[sid_y].perms.mode.value == "default"
    # the setting the command just wrote did apply
    assert server.config.context["compact_at_ratio"] == 0.6
    for rpc_name in ("reload.env", "reload.mcp"):
        await rpc(server, rpc_name, {"session_id": sid})
        assert server.config.permission_mode == "ask" and server.config.max_turns == 0, rpc_name
    await server.close()


# ── H6: a lone surrogate must not stop persistence ────────────────────────────────────────────────────────────


async def test_lone_surrogate_prompt_still_persists(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    await submit_and_wait(server, "caf\udce9 first")
    await submit_and_wait(server, "second turn")
    stored = server.store.get(sid)
    assert stored is not None and len(stored.messages) >= 4
    assert "second turn" in json.dumps(stored.messages)
    await server.close()


async def test_bad_session_title_does_not_poison_saves(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    res = await rpc(server, "session.title", {"session_id": sid, "title": "t\ud800itle"})
    assert "error" not in res, res
    await submit_and_wait(server, "hello")
    stored = server.store.get(sid)
    assert stored is not None and stored.messages and stored.title.startswith("t")
    stored.title.encode("utf-8")
    await server.close()


def test_store_survives_surrogates_in_every_column(tmp_path: Path) -> None:
    from k3code.gateway.sessions import SessionStore

    store = SessionStore(tmp_path / "s.db")
    s = store.create(title="a\ud800b", model="m\udce9", cwd=str(tmp_path))
    s.messages = [{"role": "tool", "content": "caf\udce9.txt"}]
    s.meta = {"k": "\udc80"}
    store.save(s)
    back = store.get(s.session_id)
    assert back is not None and back.messages[0]["content"].startswith("caf") and back.title.startswith("a")
    store.close()


async def test_persist_failure_is_reported_once(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)

    def boom(_stored) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(server.store, "save", boom)
    await submit_and_wait(server, "one")
    await submit_and_wait(server, "two")
    shown = [
        f
        for f in frames_of(server)
        if f.get("method") == "event" and f["params"]["type"] == "notification.show" and "save" in str(f).lower()
    ]
    assert len(shown) == 1, shown
    assert sid in server.live
    await server.close()


# ── H7: one malformed frame never ends the loop ───────────────────────────────────────────────────────────────

BAD_FRAMES = [
    '{"jsonrpc":"2.0","id":1,"method":[]}',
    '{"jsonrpc":"2.0","id":1,"method":{}}',
    '{"jsonrpc":"2.0","id":[1],"method":"session.list"}',
    "[" * 200_000,
    '{"jsonrpc":"2.0","id":1,"method":"session.list","params":5}',
    '{"jsonrpc":"2.0","id":7,"method":5}',
    '{"jsonrpc":"1.0","id":7,"method":"session.list"}',
]


@pytest.mark.parametrize("frame", BAD_FRAMES)
async def test_malformed_frame_does_not_kill_the_read_loop(env: Path, monkeypatch, frame: str) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    tasks: set[asyncio.Task] = set()
    await server._take_frame(frame, server._stdio_client, tasks)  # must not raise
    await server._take_frame(
        json.dumps({"jsonrpc": "2.0", "id": 42, "method": "session.list", "params": {}}), server._stdio_client, tasks
    )
    answers = [f for f in frames_of(server) if f.get("id") == 42]
    assert answers and "result" in answers[0]
    await server.close()


async def test_invalid_request_echoes_its_id_and_uses_spec_codes(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    tasks: set[asyncio.Task] = set()
    for line, want_id, want_code in (
        ('{"jsonrpc":"2.0","id":7,"method":5}', 7, -32600),
        ('{"jsonrpc":"1.0","id":"abc","method":"session.list"}', "abc", -32600),
        ('{"jsonrpc":"2.0","id":8,"method":"session.list","params":5}', 8, -32602),
        ('{"jsonrpc":"2.0","id":9,"method":"session.list","params":"x"}', 9, -32602),
    ):
        n = len(frames_of(server))
        await server._take_frame(line, server._stdio_client, tasks)
        out = frames_of(server)[n:]
        assert out and out[0]["id"] == want_id and out[0]["error"]["code"] == want_code, (line, out)
    await server.close()


def test_decode_frame_rejects_without_raising() -> None:
    assert decode_frame("[" * 200_000)[0] is None
    obj, err = decode_frame('{"jsonrpc":"2.0","id":1,"method":[]}')
    assert err and obj is not None and obj["id"] == 1  # the dict rides along so the reply can carry the id
    assert "invalid params" in decode_frame('{"jsonrpc":"2.0","id":1,"method":"a","params":[1]}')[1]
    assert decode_frame('{"jsonrpc":"2.0","method":"a"}')[1] is None  # a notification


# ── H8 (server half) and the L3 / L8 edges ────────────────────────────────────────────────────────────────────


async def test_session_that_cannot_start_leaves_no_stored_row(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch, permission_mode="bogus")
    res = await rpc(server, "session.create", {"cwd": str(env)})
    assert "error" in res and "bogus" in res["error"]["message"]
    assert server.store.list() == []
    await server.close()


async def test_slash_exec_with_an_empty_command_is_invalid_params(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    for command in ("/", "   ", "/ "):
        res = await rpc(server, "slash.exec", {"command": command})
        assert res["error"]["code"] == -32602, (command, res)
    await server.close()


async def test_gateway_rejects_bad_param_types_with_invalid_params(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    assert (await rpc(server, "session.create", {"cwd": 5}))["error"]["code"] == -32602
    sid = await new_session(server, env)
    assert (await rpc(server, "session.list", {"limit": "abc"}))["error"]["code"] == -32602
    res = await rpc(server, "complete.path", {"word": "~nouser-k3/x"})
    assert res["result"] == {"items": []}
    res = await rpc(server, "prompt.submit", {"text": "hi", "model": "typo"})
    assert res["error"]["code"] == -32602 and server.live[sid].stored.model != "typo"
    await server.close()


async def test_workspace_move_updates_the_permission_state(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    dest = env / "elsewhere"
    dest.mkdir()
    res = await rpc(server, "session.workspace.move", {"path": str(dest)})
    assert res["result"]["ok"], res
    assert server.live[sid].perms.cwd == dest.resolve()
    await server.close()


async def test_a_frame_with_a_lone_surrogate_is_still_delivered(env: Path, monkeypatch) -> None:
    from k3code.gateway.protocol import encode_error, encode_event, encode_response, encode_server_request

    for line in (
        encode_event("tool.complete", {"result": "ls: caf\udce9.txt"}),
        encode_response(1, {"files": ["caf\udce9.txt"]}),
        encode_error(1, -32603, "bad \ud800 name"),
        encode_server_request("clarify", {"q": "\udc80"}),
    ):
        line.encode("utf-8")  # what a socket or stdout write does; it raised, and the frame was dropped unseen
        assert "�" in line
    server, _ = make_server(env / "work", monkeypatch)
    server.emit("probe", {"name": "caf\udce9.txt"})
    assert any(f["params"]["type"] == "probe" for f in frames_of(server) if f.get("method") == "event")
    await server.close()
