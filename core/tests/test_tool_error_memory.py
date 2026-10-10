"""E1: tool failures are recorded per project; a recurring one becomes a gotcha that reaches the system prompt."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

from k3code.config import Settings
from k3code.learning import gotchas
from k3code.learning.decisions import DecisionLog
from k3code.prompting import build_system_prompt
from k3code.providers.types import ToolCall
from k3code.toolerrors import failure_of
from test_learning_gateway import make_server, shown
from test_permissions_gateway import call, run_turn

FAKE_KEY = "sk-" + "z" * 24  # built at run time: the secret scan must not see a literal


def _session(server, cwd, sid="s1"):
    return SimpleNamespace(session_id=sid, perms=SimpleNamespace(cwd=cwd), background=False, emit=lambda *a, **k: None)


def _fail(hub, sess, command, stderr, code=127):
    tc = ToolCall(id="x", name="bash", arguments={"command": command})
    result = {"stdout": "", "stderr": stderr, "exit_code": code}
    hub.tool_outcome(sess, tc, result, failure_of("bash", tc.arguments, result))


def _ok(hub, sess, command):
    tc = ToolCall(id="y", name="bash", arguments={"command": command})
    hub.tool_outcome(sess, tc, {"stdout": "fine", "stderr": "", "exit_code": 0}, None)


async def test_failures_are_recorded_scrubbed_with_class_and_project(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    hub = server.learning
    _fail(hub, _session(server, tmp_path), "deploy --token x", f"error: bad credentials {FAKE_KEY} at /srv/a/b.py:12")
    (row,) = hub.log.query("tool_error", actor=None)
    assert row["choice"] == "exit 127" and row["detail"]["tool"] == "bash"
    assert row["subject"] == "deploy: exit <n>: error: bad credentials <redacted> at <path>:<n>"
    assert FAKE_KEY not in json.dumps(row)
    assert row["project"] == hub.log.project_for(str(tmp_path))
    # a non-zero exit with nothing on stderr (a failing test run, grep without a match) is not a tool error
    tc = ToolCall(id="g", name="bash", arguments={"command": "grep -q x f"})
    res = {"stdout": "", "stderr": "", "exit_code": 1}
    hub.tool_outcome(_session(server, tmp_path), tc, res, failure_of("bash", tc.arguments, res))
    assert len(hub.log.query("tool_error", actor=None)) == 1


async def test_third_repeat_in_a_project_proposes_a_gotcha_with_the_followup_hint(tmp_path, monkeypatch):
    # learning.auto_gotchas off: today's behaviour, even a hinted failure waits for the card at REPEATS
    server, _ = make_server(tmp_path, ["ok"], monkeypatch, learning={"auto_gotchas": False})
    hub = server.learning
    s = _session(server, tmp_path)
    _fail(hub, s, "npm test", "sh: 1: npm: not found")
    _ok(hub, s, "pnpm test")  # looks like a retry of it (shares "test"): its hint is kept
    _fail(hub, _session(server, tmp_path, "s2"), "npm run lint", "sh: 1: npm: not found")
    _ok(hub, _session(server, tmp_path, "s2"), "ls")  # unrelated: no hint
    assert not [p for p in hub.store.all() if p.kind == "project_gotcha"]
    _fail(hub, _session(server, tmp_path, "s3"), "npm ci", "sh: 1: npm: not found")
    (p,) = [p for p in hub.store.all() if p.kind == "project_gotcha"]
    assert p.text == "project gotcha: npm: exit <n>: sh: <n>: npm: not found — `pnpm test` worked instead"
    _fail(hub, s, "npm x", "sh: 1: npm: not found")
    assert len([p for p in hub.store.all() if p.kind == "project_gotcha"]) == 1  # proposed once
    assert not gotchas.gotchas_path(hub.log.project_for(str(tmp_path))).exists()  # nothing written without the card


def _recording_session(cwd, sid, frames):
    return SimpleNamespace(
        session_id=sid, perms=SimpleNamespace(cwd=cwd), background=False, emit=lambda t, p: frames.append((t, p))
    )


async def test_a_hinted_failure_seen_twice_is_learned_without_a_card_and_announced(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    hub = server.learning
    frames: list = []
    s1 = _recording_session(tmp_path, "s1", frames)
    _fail(hub, s1, "python x.py", "sh: 1: python: not found")
    _ok(hub, s1, "python3 x.py")  # a retry that worked: self-verified evidence
    path = gotchas.gotchas_path(hub.log.project_for(str(tmp_path)))
    assert not path.exists()  # seen once: not yet
    s2 = _recording_session(tmp_path, "s2", frames)
    _fail(hub, s2, "python y.py", "sh: 1: python: not found")
    line = "- bash: python: exit <n>: sh: <n>: python: not found — `python3 x.py` worked instead"
    assert path.read_text(encoding="utf-8").splitlines() == [line]
    assert not [p for p in hub.store.all() if p.kind == "project_gotcha"]  # no card
    assert [p["text"] for t, p in frames if t == "notification"] == ["Learned for this project: " + line[2:]]
    _fail(hub, s2, "python z.py", "sh: 1: python: not found")  # already learned: no rewrite, no second notification
    assert path.read_text(encoding="utf-8").splitlines() == [line]
    assert len([t for t, _ in frames if t == "notification"]) == 1
    assert line in build_system_prompt("base", cwd=tmp_path, config=Settings())


async def test_a_hintless_recurring_failure_still_waits_for_the_card(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    hub = server.learning
    for i in range(2):
        _fail(hub, _session(server, tmp_path, f"s{i}"), f"npm t{i}", "sh: 1: npm: not found")
    assert not [p for p in hub.store.all() if p.kind == "project_gotcha"]
    _fail(hub, _session(server, tmp_path, "s3"), "npm t3", "sh: 1: npm: not found")
    assert [p.kind for p in hub.store.all() if p.kind == "project_gotcha"] == ["project_gotcha"]
    assert not gotchas.gotchas_path(hub.log.project_for(str(tmp_path))).exists()


def test_auto_learning_refuses_redacted_hints_and_non_project_classes():
    assert gotchas.auto_ok("exit 127", "`python3 x.py` worked instead")
    assert not gotchas.auto_ok("exit 127", "")
    assert not gotchas.auto_ok("exit 1", "`deploy --token <redacted>` worked instead")
    assert not gotchas.auto_ok("exit 1", f"`deploy {FAKE_KEY}` worked instead")
    for cls in gotchas.NOT_PROJECT:
        assert not gotchas.auto_ok(cls, "worked with different path")


async def test_repeats_older_than_the_window_or_in_other_projects_do_not_count(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    hub = server.learning
    sig = "npm: exit <n>: sh: <n>: npm: not found"
    pid = hub.log.project_for(str(tmp_path))
    for _ in range(2):
        hub.log.record(
            "tool_error",
            cwd=str(tmp_path),
            subject=sig,
            choice="exit 127",
            detail={"tool": "bash"},
            ts=time.time() - 15 * 86400,
            project=pid,
        )
    other = tmp_path / "other"
    other.mkdir()
    _fail(hub, _session(server, other), "npm a", "sh: 1: npm: not found")
    _fail(hub, _session(server, other), "npm b", "sh: 1: npm: not found")
    _fail(hub, _session(server, tmp_path), "npm c", "sh: 1: npm: not found")
    assert not [p for p in hub.store.all() if p.kind == "project_gotcha"]


async def test_accepting_writes_home_gotchas_and_the_prompt_carries_them(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    sess = await call(server, "session.create", {"cwd": str(tmp_path)})
    hub = server.learning
    for i in range(3):
        _fail(hub, _session(server, tmp_path, f"s{i}"), f"npm t{i}", "sh: 1: npm: not found")
    (p,) = [p for p in hub.store.all() if p.kind == "project_gotcha"]
    out = await call(
        server, "command.dispatch", {"name": "proposals", "arg": f"accept {p.id}", "session_id": sess["session_id"]}
    )
    assert "known pitfalls" in out["output"]
    path = gotchas.gotchas_path(hub.log.project_for(str(tmp_path)))
    assert path.is_file() and path.is_relative_to(os.environ["K3CODE_HOME"])
    assert not list(tmp_path.rglob("gotchas.md"))  # never a file in the repository
    prompt = build_system_prompt("base", cwd=tmp_path, config=Settings())
    assert "## Known pitfalls in this project" in prompt
    assert "```\n- bash: npm: exit <n>: sh: <n>: npm: not found\n```" in prompt


def test_gotchas_file_keeps_the_newest_thirty(tmp_path):
    path = tmp_path / "projects" / "p" / "gotchas.md"
    for i in range(35):
        gotchas.append_gotcha(path, f"pitfall {i}")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 30 and lines[0] == "- pitfall 5" and lines[-1] == "- pitfall 34"


async def test_a_turn_records_its_tool_failures_through_the_gateway(tmp_path, monkeypatch):
    reads = [ToolCall(id=f"r{i}", name="read", arguments={"path": f"gone-{i}.txt"}) for i in range(3)]
    server, _ = make_server(tmp_path, [*reads, "ok"], monkeypatch, mode="yolo")
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await run_turn(server, "go", [])
    rows = DecisionLog(tmp_path.parent / f"{tmp_path.name}-k3home").query("tool_error", actor=None)
    assert [r["subject"] for r in rows] == ["File not found: <path>"] * 3
    assert [c["kind"] for c in shown(server, "project_gotcha")] == ["project_gotcha"]


PASSWORD = "S3cr3t" + "Passw0rd"
DIGIT_KEY = "sk-proj-" + "1234567890" + "abcdefghijkl"


async def test_digit_bearing_credentials_never_reach_the_signature_or_the_log(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    hub = server.learning
    url = "https://u:" + PASSWORD + "@host.example/x.git"
    _fail(hub, _session(server, tmp_path), "git pull", f"fatal: Authentication failed for '{url}' key {DIGIT_KEY}")
    (row,) = hub.log.query("tool_error", actor=None)
    signature = failure_of("db", {}, {"error": f"{url} {DIGIT_KEY}"}).signature
    for secret in (PASSWORD, DIGIT_KEY, "1234567890"):
        assert secret not in json.dumps(row)
        assert secret not in signature
