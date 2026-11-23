"""`k3code bugs`, the /bugs slash command and the doctor's bugs check."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

from click.testing import CliRunner

from k3code import bugread
from k3code.cli import cli
from m1cmd_helpers import cmd, make_server, new_session

A, B, OLD = "a" * 16, "b" * 16, "0" * 16


def _write(*rows: dict) -> None:
    home = Path(os.environ["K3CODE_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    with (home / "bugs.jsonl").open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _row(fp: str, ts: float, summary: str = "boom") -> dict:
    return {"ts": ts, "kind": "test", "fp": fp, "summary": summary, "traceback": "", "ctx": {}}


def _seed() -> None:
    now = time.time()
    _write(_row(A, now - 100, "alpha broke"), _row(B, now - 50, "beta broke"), _row(OLD, now - 10 * 86400, "old"))


def _run(*args: str):
    return CliRunner().invoke(cli, ["bugs", *args])


def test_cli_lists_open_groups_in_the_window_newest_first():
    _seed()
    res = _run()
    assert res.exit_code == 0, res.output
    assert "2 open bug group(s)" in res.output
    assert res.output.index(B) < res.output.index(A) and OLD not in res.output
    assert OLD in _run("--since", "30d").output


def test_cli_json_lists_group_fields():
    _seed()
    res = _run("--json")
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert [g["fp"] for g in out] == [B, A]
    assert out[0]["count"] == 1 and out[0]["open"] is True and out[0]["summary"] == "beta broke"


def test_cli_rejects_a_bad_since():
    assert _run("--since", "soon").exit_code == 2


def test_cli_show_prints_the_latest_record_and_fails_on_an_unknown_fp():
    _seed()
    res = _run("show", A)
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["summary"] == "alpha broke"
    missing = _run("show", "f" * 16)
    assert missing.exit_code == 1 and "Unknown bug group" in missing.output


def test_cli_resolve_one_then_all_and_needs_exactly_one_target():
    _seed()
    res = _run("resolve", A)
    assert res.exit_code == 0 and A in res.output
    assert A not in _run().output
    assert _run("resolve", "f" * 16).exit_code == 1
    assert _run("resolve").exit_code == 2 and _run("resolve", B, "--all").exit_code == 2
    assert _run("resolve", "--all").exit_code == 0
    assert bugread.groups() == []
    assert "No open bug groups" in _run().output


def test_cli_export_writes_a_private_brief_to_path(tmp_path):
    _seed()
    out = tmp_path / "brief.md"
    res = _run("export", str(out), "--since", "30d")
    assert res.exit_code == 0, res.output
    assert str(out) in res.output
    text = out.read_text()
    assert text.startswith(bugread.HEADER) and A in text and OLD in text
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


async def test_slash_bugs_lists_shows_resolves_and_exports(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    _seed()
    listed = await cmd(server, "/bugs", sid)
    assert listed["fps"] == [B, A] and "2 open bug group(s)" in listed["output"]
    assert [g["fp"] for g in json.loads((await cmd(server, "/bugs --json", sid))["output"])] == [B, A]
    assert json.loads((await cmd(server, f"/bugs show {A}", sid))["output"])["summary"] == "alpha broke"
    assert "Resolved 1" in (await cmd(server, f"/bugs resolve {A}", sid))["output"]
    assert (await cmd(server, "/bugs", sid))["fps"] == [B]
    exported = await cmd(server, "/bugs export brief.md", sid)
    assert exported["path"] == str(tmp_path / "brief.md") and B in (tmp_path / "brief.md").read_text()
    assert (await cmd(server, "/bugs nonsense", sid))["output"].startswith("Usage:")


def test_bugs_is_a_registered_headless_light_command_in_the_system_group():
    from k3code.commands.builtin import build_registry
    from k3code.gateway.server import HELP_GROUPS

    bugs = build_registry().get("bugs")
    assert bugs is not None and bugs.headless and not bugs.needs_server
    assert "bugs" in dict(HELP_GROUPS)["System"]


async def _doctor_bugs():
    from k3code import doctor
    from k3code.config import Settings

    checks = {c.name: c for c in await doctor.run_checks(Settings(), probe=False, home=Path(os.environ["K3CODE_HOME"]))}
    return doctor, checks["bugs"]


async def test_doctor_warns_on_an_open_bug_group():
    _write(_row(A, time.time() - 100))
    doctor, check = await _doctor_bugs()
    assert check.status == doctor.WARN
    assert check.detail == "1 open bug group(s) in 7 days, run k3code bugs"
    assert check.data["open"] == 1


async def test_doctor_is_ok_with_no_open_group_in_the_window():
    now = time.time()
    _write(_row(OLD, now - 10 * 86400), _row(A, now - 100))
    bugread.resolve(A)
    doctor, check = await _doctor_bugs()
    assert check.status == doctor.OK and check.data["open"] == 0
