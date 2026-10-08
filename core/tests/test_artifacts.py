"""Artifact registry, /artifacts command and the producers that register into it."""

from __future__ import annotations

from pathlib import Path

from k3code.artifacts import ArtifactStore, slugify, write_artifact_file
from test_autonomy_gateway import PLAN, call, events, make, start, verdict  # noqa: F401


def test_store_register_list_get_prefix(tmp_path):
    store = ArtifactStore(tmp_path / "a.db")
    f = tmp_path / "plan.md"
    f.write_text("x")
    a = store.register("plan", f, title="my plan", session="s1")
    store.register("research", tmp_path / "r.md", title="rep", session="s2")
    assert [x.kind for x in store.list()] == ["research", "plan"]
    assert [x.id for x in store.list(kind="plan")] == [a.id]
    assert [x.id for x in store.list(session="s1")] == [a.id]
    assert store.get(a.id[:4]).id == a.id and store.get("nope") is None
    assert store.register("weird", f).kind == "other"
    assert a.exists and not store.list(kind="research")[0].exists


def test_write_artifact_file_unique_and_registered(tmp_path):
    class Ctx:
        artifacts = ArtifactStore(tmp_path / "a.db")

    d = tmp_path / "out"
    p1 = write_artifact_file(Ctx, "plan", d, "Same Title!", "one")
    p2 = write_artifact_file(Ctx, "plan", d, "Same Title!", "two")
    assert p1 != p2 and p1.read_text() == "one" and slugify("Same Title!") in p1.name
    assert len(Ctx.artifacts.list(kind="plan")) == 2


async def test_artifacts_command_list_open_publish(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [])
    await start(server, tmp_path)
    out = await call(server, "slash.exec", {"command": "artifacts"})
    assert "No artifacts" in out["output"]
    f = tmp_path / "x.md"
    f.write_text("hi")
    art = server.artifacts.register("plan", f, title="the plan")
    out = await call(server, "slash.exec", {"command": "artifacts"})
    assert art.id in out["output"] and "the plan" in out["output"]
    monkeypatch.setenv("EDITOR", "vim")
    out = await call(server, "slash.exec", {"command": f"artifacts open {art.id}"})
    assert str(f.resolve()) in out["output"] and "vim" in out["output"]
    out = await call(server, "slash.exec", {"command": f"artifacts publish {art.id}"})
    assert "not implemented" in out["output"]
    out = await call(server, "slash.exec", {"command": "artifacts open zzz"})
    assert "Usage" in out["output"]


async def test_producers_register_debug_dump_and_preview(tmp_path, monkeypatch):
    steps = [{"type": "text", "match": "sketch", "text": "ROUGH SKETCH"}]
    server = make(tmp_path, monkeypatch, steps)
    await start(server, tmp_path)
    await call(server, "slash.exec", {"command": "preview build a thing"})
    kinds = {a.kind for a in server.artifacts.list()}
    assert "preview" in kinds
    prev = server.artifacts.list(kind="preview")[0]
    assert "ROUGH SKETCH" in Path(prev.path).read_text()
    await call(server, "slash.exec", {"command": "debug dump"})
    assert "debug" in {a.kind for a in server.artifacts.list()}
