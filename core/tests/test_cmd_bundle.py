"""/export, /import (+CLI): redaction, round trip, backups, collisions, confirmation."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile

import yaml
from click.testing import CliRunner

from k3code.cli import cli
from k3code.gateway.sessions import SessionStore
from k3code.paths import home
from k3code.redact import REDACTED, redact
from m1cmd_helpers import cmd, frames_of, make_server, new_session

SECRET_KEY = "sk-proj-abcdef0123456789ABCDEF0123456789"
SECRET_TOKEN = "tok_9f8e7d6c5b4a39281706f5e4d3c2b1a0"
SECRET_PASS = "hunter2-correct-horse"


def write_user_config(h):
    h.mkdir(parents=True, exist_ok=True)
    (h / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "max_tokens": 4096,
                "providers": [
                    {
                        "name": "p",
                        "kind": "openai",
                        "base_url": "http://x/v1",
                        "api_key_env": "MY_API_KEY",
                        "api_key": SECRET_KEY,
                    }
                ],
                "mem0": {"url": "http://m", "api_key_env": "MEM_KEY", "user_id": "u"},
                "mcp": {"servers": {"s": {"command": "x", "env": {"GITHUB_TOKEN": SECRET_TOKEN, "FOO": "bar"}}}},
                "extra_password": SECRET_PASS,
            }
        )
    )


def test_redact_rules():
    out = redact(
        {
            "api_key": "abc",
            "api_key_env": "MY_API_KEY",
            "max_tokens": 10,
            "nested": {"password": "pw", "note": f"{SECRET_KEY}", "plain": "hello", "flag_secret": True},
            "list": ["ghp_" + "a" * 30, "fine"],
            "headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz0123"},
        }
    )
    assert out["api_key"] == REDACTED and out["api_key_env"] == "MY_API_KEY"
    assert out["max_tokens"] == 10
    assert out["nested"]["password"] == REDACTED and out["nested"]["note"] == REDACTED
    assert out["nested"]["plain"] == "hello" and out["nested"]["flag_secret"] is True
    assert out["list"] == [REDACTED, "fine"]
    assert out["headers"]["Authorization"] == REDACTED


async def test_export_import_round_trip(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path / "a", monkeypatch)
    write_user_config(home())
    proj = tmp_path / "proj"
    (proj / ".k3code").mkdir(parents=True)
    (proj / ".k3code" / "config.yaml").write_text(yaml.safe_dump({"output_style": "concise"}))
    sid = await new_session(server, proj)
    stored = server.store.get(sid)
    stored.messages = [{"role": "user", "content": f"my key is {SECRET_KEY}"}, {"role": "assistant", "content": "ok"}]
    stored.title = "first"
    server.store.save(stored)
    out = tmp_path / "x.k3bundle"

    res = await cmd(server, f"/export --all {out}", sid)
    assert "Exported 1 session" in res["output"], res

    with tarfile.open(out) as tar:
        names = tar.getnames()
        blob = b"".join(tar.extractfile(n).read() for n in names if tar.getmember(n).isfile())
    assert "manifest.json" in names and f"sessions/{sid}.json" in names
    assert "settings/user.config.yaml" in names and "settings/project.config.yaml" in names
    for secret in (SECRET_KEY, SECRET_TOKEN, SECRET_PASS):
        assert secret.encode() not in blob
    assert b"MY_API_KEY" in blob and b"max_tokens: 4096" in blob
    await server.close()

    # Fresh install: has its own config (with a real secret that must survive) and no sessions.
    server2, _ = make_server(tmp_path / "b", monkeypatch)
    h2 = home()
    h2.mkdir(parents=True, exist_ok=True)
    (h2 / "config.yaml").write_text(yaml.safe_dump({"mem0": {"api_key": "LOCAL-SECRET-VALUE"}, "max_turns": 7}))
    sid2 = await new_session(server2, proj)
    res = await cmd(server2, f"/import {out} --yes", sid2)
    assert res["output"].startswith("Imported."), res
    merged = yaml.safe_load((h2 / "config.yaml").read_text())
    assert merged["max_turns"] == 7 and merged["max_tokens"] == 4096
    assert merged["mem0"]["api_key"] == "LOCAL-SECRET-VALUE"  # <redacted> never overwrites
    assert merged["mem0"]["url"] == "http://m"
    assert REDACTED not in (h2 / "config.yaml").read_text()
    assert list(h2.glob("config.yaml.bak-*")), "existing config must be backed up"
    imported = server2.store.get(sid)  # id was free in the new store → kept
    assert imported is not None and imported.title == "first"
    assert SECRET_KEY not in json.dumps(imported.messages)
    assert (proj / ".k3code" / "config.yaml").is_file()

    res = await cmd(server2, f"/import {out} --yes --session-only", sid2)  # collision → new id
    assert "id collided" in res["output"]
    assert len([s for s in server2.store.list() if s.title == "first"]) == 2
    await server2.close()


async def test_import_asks_for_confirmation(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path / "a", monkeypatch)
    sid = await new_session(server, tmp_path)
    out = tmp_path / "c.k3bundle"
    await cmd(server, f"/export --session-only {out}", sid)

    task = asyncio.create_task(cmd(server, f"/import {out} --session-only", sid))
    for _ in range(200):
        reqs = [f for f in frames_of(server) if f.get("method") == "clarify"]
        if reqs:
            break
        await asyncio.sleep(0.01)
    assert reqs and "Import" in reqs[0]["params"]["choices"]
    await rpc_answer(server, reqs[0]["id"], "Cancel")
    assert "cancelled" in (await task)["output"].lower()
    await server.close()


async def rpc_answer(server, req_id, answer):
    await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"answer": answer}}))


async def test_import_rejects_garbage(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path / "a", monkeypatch)
    bad = tmp_path / "bad.k3bundle"
    bad.write_bytes(b"nope")
    res = await cmd(server, f"/import {bad} --yes")
    assert "Import failed" in res["output"]
    # path traversal names are ignored, never extracted
    evil = tmp_path / "evil.k3bundle"
    with tarfile.open(evil, "w:gz") as tar:
        for name, data in (("manifest.json", b'{"version": 1, "contents": {}}'), ("../../etc/x", b"boom")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    res = await cmd(server, f"/import {evil} --yes")
    assert res["output"].startswith("Imported.")
    await server.close()


def test_import_drops_mode_roots_and_background_markers_from_session_meta(tmp_path):
    from k3code.bundle import Bundle, apply_bundle

    store = SessionStore(tmp_path / "s.db")
    meta = {
        "mode": "yolo", "add_dirs": ["/"], "background": True, "origin": "automation", "origin_session": "abc",
        "output_style": "concise",
    }
    bundle = Bundle(manifest={"version": 1}, sessions=[{"session_id": "s1", "title": "t", "meta": meta}])
    apply_bundle(bundle, store=store, cwd=tmp_path, settings=False)
    assert store.get("s1").meta == {"output_style": "concise"}


def test_cli_export_import(tmp_path, monkeypatch):
    h = tmp_path / "h"
    monkeypatch.setenv("K3CODE_HOME", str(h))
    write_user_config(h)
    store = SessionStore(h / "sessions.db")
    s = store.create(title="cli", cwd=str(tmp_path))
    s.messages = [{"role": "user", "content": "hi"}]
    store.save(s)
    store.close()
    runner = CliRunner()
    out = tmp_path / "x.k3bundle"
    r = runner.invoke(cli, ["export", "--all", str(out)])
    assert r.exit_code == 0 and "Exported 1 session" in r.output, r.output
    with tarfile.open(out) as tar:
        blob = b"".join(tar.extractfile(n).read() for n in tar.getnames() if tar.getmember(n).isfile())
    assert SECRET_KEY.encode() not in blob and SECRET_TOKEN.encode() not in blob

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "h2"))
    r = runner.invoke(cli, ["import", str(out), "--yes"])
    assert r.exit_code == 0 and "Imported." in r.output, r.output
    r = runner.invoke(cli, ["import", str(out)], input="n\n")  # no --yes → asks → abort
    assert r.exit_code != 0
