"""Memory in a .k3bundle: /export and /import (+CLI) carry USER.md, learned notes and gotchas; /memory show."""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code.bundle import Bundle, BundleError, _memory_from, apply_bundle, pending_sensitive, read_bundle
from k3code.cli import cli
from k3code.commands.export import run_export
from k3code.gateway.sessions import SessionStore
from k3code.learning.decisions import project_id
from k3code.learning.gotchas import _entries, append_gotcha, auto_gotchas_path, gotchas_path, user_gotchas_path
from k3code.memory import read_learned, user_memory_path, write_learned
from k3code.redact import REDACTED
from m1cmd_helpers import cmd, make_server, new_session

SHAPED = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0" * 2  # a GitHub token by its shape
PLAIN = "plain-value-9x"  # no shape: only the env value it is masks it


@pytest.fixture(autouse=True)
def _no_git_env(monkeypatch):
    """project_id runs git: never against a repository named by the environment (a hook's GIT_DIR)."""
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)


def _new_home(tmp_path: Path, monkeypatch, name: str) -> Path:
    h = tmp_path / name
    h.mkdir()
    monkeypatch.setenv("K3CODE_HOME", str(h))
    return h


def _seed(proj: Path) -> None:
    """USER.md (with secrets), learned notes, the three gotchas files and instruction files in the project."""
    usr = user_memory_path()
    usr.parent.mkdir(parents=True, exist_ok=True)
    usr.write_text(f"# Me\n- prefers tabs\n- my token is {SHAPED}\n- the key is {PLAIN}\nprose line\n")
    write_learned(proj, ["tests run with uv", "the API lives in core/"])
    pid = project_id(proj)
    append_gotcha(gotchas_path(pid), "bash: npm test fails — use pnpm")
    append_gotcha(auto_gotchas_path(pid), "bash: make fails — use just")
    append_gotcha(user_gotchas_path(), "bash: sudo hangs")
    (proj / "K3CODE.md").write_text("PROJECT INSTRUCTIONS TEXT\n")
    (proj / ".k3code" / "rules").mkdir(parents=True)
    (proj / ".k3code" / "rules" / "style.md").write_text("RULES TEXT\n")


def _members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def _export(tmp_path: Path, monkeypatch, **flags) -> tuple[Path, Path, dict]:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    monkeypatch.setenv("MYAPP_TOKEN", PLAIN)
    _seed(proj)
    store = SessionStore(tmp_path / "s.db")
    s = store.create(title="t", cwd=str(proj))
    store.save(s)
    out, manifest = run_export(store, proj, path=str(tmp_path / "x.k3bundle"), current=s.session_id, **flags)
    store.close()
    return proj, out, manifest


def _tar(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_export_carries_memory_with_secrets_masked(tmp_path, monkeypatch):
    proj, out, manifest = _export(tmp_path, monkeypatch)
    files = _members(out)
    blob = b"".join(files.values())
    assert SHAPED.encode() not in blob and PLAIN.encode() not in blob
    assert b"PROJECT INSTRUCTIONS TEXT" not in blob and b"RULES TEXT" not in blob
    mem = json.loads(files["memory.json"])
    assert mem["version"] == 1 and mem["project_id"] == project_id(proj)
    assert "- prefers tabs" in mem["user"] and REDACTED in mem["user"]
    assert mem["learned"] == ["tests run with uv", "the API lives in core/"]
    assert mem["gotchas"] == ["- bash: npm test fails — use pnpm"]
    assert mem["gotchas_auto"] == ["- bash: make fails — use just"]
    assert mem["user_gotchas"] == ["- bash: sudo hangs"]
    assert [x["name"] for x in mem["instructions"]] == ["K3CODE.md", ".k3code/rules/style.md"]
    assert mem["instructions"][0]["bytes"] == len("PROJECT INSTRUCTIONS TEXT\n")
    contents = manifest["contents"]
    assert contents["memory"] == ["user", "learned", "gotchas", "gotchas_auto", "user_gotchas", "instructions"]
    assert json.loads(files["manifest.json"])["contents"]["memory"] == contents["memory"]
    assert f"sessions/{contents['sessions'][0]}.json" in files


def test_no_memory_leaves_it_out(tmp_path, monkeypatch):
    _proj, out, manifest = _export(tmp_path, monkeypatch, no_memory=True)
    assert "memory.json" not in _members(out) and manifest["contents"]["memory"] == []
    assert manifest["contents"]["sessions"]


def test_memory_only_exports_memory_alone(tmp_path, monkeypatch):
    (Path(os.environ["K3CODE_HOME"]) / "config.yaml").write_text("max_tokens: 4096\n")
    _proj, out, manifest = _export(tmp_path, monkeypatch, memory_only=True)
    assert set(_members(out)) == {"manifest.json", "memory.json"}
    assert manifest["contents"]["settings"] == [] and manifest["contents"]["sessions"] == []


def test_settings_only_and_session_only_carry_no_memory(tmp_path, monkeypatch):
    for flag in ("settings_only", "session_only"):
        _proj, out, manifest = _export(tmp_path, monkeypatch, **{flag: True})
        assert "memory.json" not in _members(out) and manifest["contents"]["memory"] == [], flag
        (tmp_path / "proj" / ".k3code" / "rules" / "style.md").unlink()
        (tmp_path / "proj" / ".k3code" / "rules").rmdir()


@pytest.mark.parametrize(
    "flags",
    [
        {"memory_only": True, "no_memory": True},
        {"memory_only": True, "settings_only": True},
        {"memory_only": True, "session_only": True},
        {"settings_only": True, "session_only": True},
    ],
)
def test_contradictory_export_flags_are_refused(tmp_path, flags):
    store = SessionStore(tmp_path / "s.db")
    with pytest.raises(BundleError, match="exclude each other"):
        run_export(store, tmp_path, path=str(tmp_path / "x.k3bundle"), **flags)
    assert not (tmp_path / "x.k3bundle").exists()


def _fresh_import(tmp_path, monkeypatch, out: Path, **kw):
    _new_home(tmp_path, monkeypatch, f"h{len(list(tmp_path.glob('h*')))}")
    bundle = read_bundle(out)
    return bundle, apply_bundle(bundle, store=SessionStore(tmp_path / "s2.db"), cwd=tmp_path / "proj", **kw)


def test_user_memory_is_not_applied_without_a_yes(tmp_path, monkeypatch):
    _proj, out, _ = _export(tmp_path, monkeypatch)
    _bundle, rep = _fresh_import(tmp_path, monkeypatch, out, sessions=False)
    assert not user_memory_path().exists()
    assert "memory.user" in rep.skipped
    assert "warning: skipped memory.user" in rep.describe() and "system prompt" in rep.describe()


def test_user_memory_applied_on_accept_once(tmp_path, monkeypatch):
    _proj, out, _ = _export(tmp_path, monkeypatch)
    _new_home(tmp_path, monkeypatch, "fresh")
    usr = user_memory_path()
    usr.parent.mkdir(parents=True)
    usr.write_text("- prefers tabs\n")
    bundle = read_bundle(out)
    (item,) = pending_sensitive(bundle, tmp_path / "proj", settings=False)
    assert item.key == "memory.user" and item.diff == [f"+ - my token is {REDACTED}", f"+ - the key is {REDACTED}"]
    assert pending_sensitive(bundle, tmp_path / "proj", settings=False, memory=False) == []
    store = SessionStore(tmp_path / "s2.db")
    rep = apply_bundle(bundle, store=store, cwd=tmp_path / "proj", sessions=False, accept=lambda i: True)
    text = usr.read_text()
    assert "## Imported from bundle (" in text and text.count("- prefers tabs") == 1
    assert f"- my token is {REDACTED}" in text and "prose line" not in text
    assert stat.S_IMODE(usr.stat().st_mode) == 0o600
    assert "memory.user" not in rep.skipped and any("user memory: 2 line(s)" in x for x in rep.memory)
    # a second import adds nothing: no item, no duplicate, no second heading
    assert pending_sensitive(bundle, tmp_path / "proj", settings=False) == []
    apply_bundle(bundle, store=store, cwd=tmp_path / "proj", sessions=False, trust_bundle=True)
    assert usr.read_text() == text


def test_trust_bundle_applies_user_memory(tmp_path, monkeypatch):
    _proj, out, _ = _export(tmp_path, monkeypatch)
    _bundle, rep = _fresh_import(tmp_path, monkeypatch, out, sessions=False, trust_bundle=True)
    assert "- prefers tabs" in user_memory_path().read_text() and rep.skipped == []


def test_learned_notes_merged_deduped_and_cleaned(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_learned(proj, ["kept local note", "shared note"])
    raw = {"learned": ["shared note", "new note", "```rm -rf /```", "see ](http://evil)", "[docs](http://x) here"]}
    rep = apply_bundle(
        Bundle({"version": 1}, memory=_memory_from(raw)), store=SessionStore(tmp_path / "s.db"), cwd=proj
    )
    assert read_learned(proj) == ["kept local note", "shared note", "new note", "docs here"]
    assert any("learned notes: 2 added" in x for x in rep.memory)
    many = {"learned": [f"note {i}" for i in range(100)]}
    apply_bundle(Bundle({"version": 1}, memory=_memory_from(many)), store=SessionStore(tmp_path / "s.db"), cwd=proj)
    notes = read_learned(proj)
    assert len(notes) == 60 and notes[-1] == "note 99"


def test_gotchas_land_in_the_auto_file_only(tmp_path, monkeypatch):
    proj, out, _ = _export(tmp_path, monkeypatch)
    _fresh_import(tmp_path, monkeypatch, out, sessions=False)
    pid = project_id(proj)
    assert not gotchas_path(pid).exists() and not user_gotchas_path().exists()
    assert _entries(auto_gotchas_path(pid)) == [
        "- bash: npm test fails — use pnpm",
        "- bash: make fails — use just",
        "- bash: sudo hangs",
    ]
    # idempotent
    apply_bundle(read_bundle(out), store=SessionStore(tmp_path / "s3.db"), cwd=proj, sessions=False)
    assert len(_entries(auto_gotchas_path(pid))) == 3


def test_hostile_memory_json_never_crashes_and_is_capped(tmp_path):
    hostile = {
        "version": "x",
        "project_id": ["no"],
        "user": 5,
        "learned": "not a list",
        "gotchas": [1, None, {"a": 1}, "- ok line", ""],
        "gotchas_auto": [f"- g{i}" for i in range(10_000)],
        "user_gotchas": {"a": 1},
        "instructions": [1, {"name": 3}, {"name": "A.md", "bytes": "x"}, {"name": "B.md", "bytes": 4}],
    }
    path = _tar(
        tmp_path / "h.k3bundle", {"manifest.json": b'{"version": 1}', "memory.json": json.dumps(hostile).encode()}
    )
    bundle = read_bundle(path)
    assert set(bundle.memory) == {"gotchas", "gotchas_auto", "instructions"}
    assert bundle.memory["gotchas"] == ["- ok line"] and len(bundle.memory["gotchas_auto"]) == 200
    assert bundle.memory["instructions"] == [{"name": "A.md", "bytes": 0}, {"name": "B.md", "bytes": 4}]
    assert "memory:" in bundle.describe()
    proj = tmp_path / "proj"
    proj.mkdir()
    rep = apply_bundle(bundle, store=SessionStore(tmp_path / "s.db"), cwd=proj)
    assert len(_entries(auto_gotchas_path(project_id(proj)))) == 30 and rep.skipped == []
    for doc in (b"[1, 2]", b'"text"', b"null"):
        p = _tar(tmp_path / "l.k3bundle", {"manifest.json": b'{"version": 1}', "memory.json": doc})
        assert read_bundle(p).memory == {}
    p = _tar(tmp_path / "bad.k3bundle", {"manifest.json": b'{"version": 1}', "memory.json": b"{not json"})
    with pytest.raises(BundleError, match="memory.json"):
        read_bundle(p)


def test_a_bundle_without_memory_imports_as_before(tmp_path):
    session = {"session_id": "s1", "title": "old", "messages": []}
    path = _tar(
        tmp_path / "old.k3bundle",
        {"manifest.json": b'{"version": 1}', "sessions/s1.json": json.dumps(session).encode()},
    )
    bundle = read_bundle(path)
    assert bundle.memory == {} and "memory: none" in bundle.describe()
    store = SessionStore(tmp_path / "s.db")
    rep = apply_bundle(bundle, store=store, cwd=tmp_path)
    assert rep.sessions == {"s1": "s1"} and rep.memory == [] and rep.skipped == []
    assert not user_memory_path().exists()


def test_instruction_files_are_reported_never_written(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    mem = _memory_from({"instructions": [{"name": "K3CODE.md", "bytes": 12}, {"name": "../../x.md", "bytes": 1}]})
    rep = apply_bundle(Bundle({"version": 1}, memory=mem), store=SessionStore(tmp_path / "s.db"), cwd=proj)
    assert "K3CODE.md (12 bytes)" in rep.describe() and "not written" in rep.describe()
    assert list(proj.iterdir()) == [] and not (tmp_path / "x.md").exists()


def test_cli_export_and_import_memory(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    _seed(proj)
    monkeypatch.chdir(proj)
    runner = CliRunner()
    out = tmp_path / "m.k3bundle"
    r = runner.invoke(cli, ["export", "--memory-only", str(out)])
    assert r.exit_code == 0 and "memory: user, 2 learned notes, 3 gotchas" in r.output, r.output
    assert set(_members(out)) == {"manifest.json", "memory.json"}
    r = runner.invoke(cli, ["export", "--memory-only", "--no-memory", str(tmp_path / "n.k3bundle")])
    assert r.exit_code != 0 and "exclude each other" in r.output
    r = runner.invoke(cli, ["export", "--no-memory", str(tmp_path / "n.k3bundle")])
    assert r.exit_code == 0 and "memory: none" in r.output, r.output

    _new_home(tmp_path, monkeypatch, "h2")
    r = runner.invoke(cli, ["import", str(out), "--memory-only", "--settings-only"])
    assert r.exit_code == 2
    r = runner.invoke(cli, ["import", str(out), "--yes"])
    assert r.exit_code == 0 and "warning: skipped memory.user" in r.output, r.output
    assert not user_memory_path().exists() and read_learned(proj) == ["tests run with uv", "the API lives in core/"]
    r = runner.invoke(cli, ["import", str(out), "--yes", "--no-memory", "--trust-bundle"])
    assert r.exit_code == 0 and not user_memory_path().exists()
    r = runner.invoke(cli, ["import", str(out), "--yes", "--memory-only", "--trust-bundle"])
    assert r.exit_code == 0 and "- prefers tabs" in user_memory_path().read_text(), r.output


async def test_gateway_import_memory_refreshes_the_live_prompt(tmp_path, monkeypatch):
    proj, out, _ = _export(tmp_path, monkeypatch)
    server, _ = make_server(tmp_path / "srv", monkeypatch)
    _new_home(tmp_path, monkeypatch, "h3")
    sid = await new_session(server, proj)
    live = server.sessions.get(sid)
    live.prompt_frozen["user_memory"] = "old"
    live.learned_notes = ["old"]
    res = await cmd(server, f"/import {out} --yes --memory-only", sid)
    assert "warning: skipped memory.user" in res["output"], res
    assert live.learned_notes is None
    live.prompt_frozen["user_memory"] = "old"
    res = await cmd(server, f"/import {out} --yes --memory-only --trust-bundle", sid)
    assert "user memory: 3 line(s)" in res["output"], res
    assert "user_memory" not in live.prompt_frozen
    assert "Usage" in (await cmd(server, f"/import {out} --memory-only --no-memory", sid))["output"]
    res = await cmd(server, f"/export {tmp_path / 'g.k3bundle'} --memory-only --session-only", sid)
    assert "exclude each other" in res["output"]
    await server.close()


async def test_memory_show_prints_what_the_model_sees(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path / "srv", monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    _seed(proj)
    sid = await new_session(server, proj)
    res = await cmd(server, "/memory show", sid)
    text = res["output"]
    assert "- prefers tabs" in text and "PROJECT INSTRUCTIONS TEXT" in text and "RULES TEXT" in text
    assert "- tests run with uv" in text
    assert "1 accepted" in text and "1 auto-learned" in text and "1 on this machine" in text
    assert res["gotchas"] == {"accepted": 1, "auto": 1, "user": 1}
    user_memory_path().write_text("- " + "x" * 9000 + "\n")
    assert "…(clipped" in (await cmd(server, "/memory show", sid))["output"]
    assert "user:" in (await cmd(server, "/memory", sid))["output"]  # the listing is unchanged
    assert "show" in (await cmd(server, "/memory bogus", sid))["output"]
    await server.close()
