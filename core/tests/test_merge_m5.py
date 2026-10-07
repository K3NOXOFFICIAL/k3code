"""merge-m5: GOAL.md command set, /permissions, /focus, actor tagging of unattended decisions."""

from __future__ import annotations

import re
from pathlib import Path

from k3code.commands.builtin import build_registry
from k3code.confio import read_yaml
from k3code.learning.decisions import DecisionLog
from test_permissions_gateway import call, make_server

GOAL = Path(__file__).resolve().parents[2] / "GOAL.md"


def _parse(line: str) -> list[str]:
    out: list[str] = []
    for chunk in re.findall(r"`([^`]+)`", line):
        out += [c.strip() for c in chunk.split(",") if re.fullmatch(r"[a-z][a-z-]*", c.strip())]
    return out


def test_every_goal_command_registered_and_in_help(tmp_path, monkeypatch):
    names = _parse(next(ln for ln in GOAL.read_text(encoding="utf-8").splitlines() if "`goal, loop," in ln))
    assert len(names) >= 36 and "permissions" in names and "advisor" in names
    reg = build_registry()
    missing = [n for n in names if reg.get(n) is None]
    assert not missing, missing


async def test_help_lists_goal_commands_and_focus(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    out = await call(server, "command.dispatch", {"name": "help", "arg": ""})
    text = out["output"]
    names = _parse(next(ln for ln in GOAL.read_text(encoding="utf-8").splitlines() if "`goal, loop," in ln))
    for n in [*names, "focus"]:
        assert re.search(rf"/{re.escape(n)}\b", text), n


async def test_focus_command_toggles_config(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    r = await call(server, "config.set", {"key": "display.focus", "value": "on"})
    assert r["ok"] and server.config.display.focus_mode is True
    await call(server, "command.dispatch", {"name": "focus", "arg": "off"})
    assert server.config.display.focus_mode is False


async def test_permissions_show_add_remove(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    sess = await call(server, "session.create", {"cwd": str(tmp_path)})
    sid = sess["session_id"]

    async def perm(arg):
        res = await call(server, "command.dispatch", {"name": "permissions", "arg": arg, "session_id": sid})
        return res["output"]

    shown = await perm("")
    assert "Mode: default" in shown and "[builtin]" in shown and "rm-rf-root" in shown
    assert "added" in await perm("deny bash terraform apply*")
    assert read_yaml(tmp_path / ".k3code" / "config.yaml")["permissions"]["bash"]["terraform apply*"] == "deny"
    line = next(ln for ln in (await perm("")).splitlines() if "terraform apply*" in ln)
    assert "[project]" in line
    rid = line.split()[0]
    assert "Removed" in await perm(f"rm {rid}")
    assert "terraform" not in await perm("")
    assert "added to session" in await perm("allow bash echo * --session")
    assert "[session]" in await perm("")
    assert "plan" in await perm("mode plan")
    assert "Built-in" in await perm("rm b1")


def test_miners_ignore_auto_actor(tmp_path):
    log = DecisionLog(tmp_path)
    log.record("approval", subject="npm test *", choice="once", cwd="/p")
    log.record("approval", subject="npm test *", choice="once", cwd="/p", actor="auto")
    assert len(log.query("approval")) == 1
    assert len(log.query("approval", actor=None)) == 2
    assert log.query("approval", actor="auto")[0]["detail"]["actor"] == "auto"


async def test_background_session_records_auto(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    sess = await call(server, "session.create", {"cwd": str(tmp_path)})
    live = server.sessions.get(sess["session_id"])
    live.background = True
    server.learning.approval(live, "bash", "echo *", "once")
    live.background = False
    server.learning.approval(live, "bash", "echo *", "once")
    rows = server.learning.log.query("approval", actor=None)
    assert [r["detail"]["actor"] for r in rows] == ["auto", "user"]
