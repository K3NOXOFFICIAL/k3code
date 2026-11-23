"""k3code.bugread: grouping, resolve, show, the Markdown brief and the TUI crash log as a second source."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from k3code import bugread


def _home() -> Path:
    return Path(os.environ["K3CODE_HOME"])


def _write(*rows: dict | str) -> None:
    """Rows straight into bugs.jsonl, unscrubbed: what an older writer, or a scrub that missed, left there."""
    with (_home() / "bugs.jsonl").open("a", encoding="utf-8") as f:
        for row in rows:
            f.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


def _row(fp: str, ts: float, summary: str = "boom", **extra) -> dict:
    return {"ts": ts, "kind": "test", "fp": fp, "summary": summary, "traceback": "", "ctx": {}, **extra}


def test_rows_group_by_fp_with_counts_and_suppressed_sums_and_malformed_lines_skipped():
    now = time.time()
    _write(
        _row("a" * 16, now - 300, "first", suppressed=2),
        "{not json",
        '["a list"]',
        '{"fp": "nots", "ts": "yesterday"}',
        _row("b" * 16, now - 200),
        _row("a" * 16, now - 100, "latest", suppressed=3),
    )
    by_fp = {g.fp: g for g in bugread.groups()}
    assert set(by_fp) == {"a" * 16, "b" * 16}
    a = by_fp["a" * 16]
    assert (a.count, a.suppressed, a.summary, a.kind) == (2, 5, "latest", "test")
    assert (a.first_seen, a.last_seen) == (now - 300, now - 100)
    assert by_fp["b" * 16].count == 1


def test_resolve_hides_a_group_and_a_newer_record_reopens_it():
    now = time.time()
    _write(_row("c" * 16, now - 100), _row("d" * 16, now - 100))
    assert bugread.resolve("c" * 16) == ["c" * 16]
    assert [g.fp for g in bugread.groups()] == ["d" * 16]
    assert {g.fp for g in bugread.groups(include_resolved=True)} == {"c" * 16, "d" * 16}
    resolved_ts = max(
        r["ts"] for r in bugread.parse_rows((_home() / "bugs.jsonl").read_text()) if r["kind"] == "resolved"
    )
    _write(_row("c" * 16, resolved_ts + 1, "again"))
    assert {g.fp for g in bugread.groups()} == {"c" * 16, "d" * 16}
    assert sorted(bugread.resolve("all")) == ["c" * 16, "d" * 16]
    assert bugread.resolve("0" * 16) == []


def test_show_returns_the_latest_record_of_the_group():
    now = time.time()
    _write(_row("e" * 16, now - 100, "old", tool="t1"), _row("e" * 16, now - 50, "new", tool="t2"))
    record = bugread.show("e" * 16)
    assert record is not None and record["summary"] == "new" and record["tool"] == "t2"
    assert bugread.show("f" * 16) is None


def test_export_writes_a_private_brief_with_header_and_a_section():
    now = time.time()
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/srv/app/k3code/router.py", line 10, in route\n'
        '  File "/srv/app/k3code/providers/openai.py", line 42, in call\n'
        "RuntimeError: upstream died\n"
    )
    _write(_row("1" * 16, now - 60, "RuntimeError: upstream died", traceback=tb, ctx={"turn": "3", "mode": "x"}))
    _write(_row("2" * 16, now - 30 * 86400, "too old"))
    path = bugread.export()
    assert path.parent == _home() / "bugs" and path.name.startswith("brief-") and path.suffix == ".md"
    assert os.stat(path).st_mode & 0o777 == 0o600
    brief = path.read_text(encoding="utf-8")
    assert brief.startswith(bugread.HEADER)
    assert "## 1. test: RuntimeError: upstream died" in brief
    assert "k3code/providers/openai.py:42" in brief and "ctx keys: mode, turn" in brief
    assert "too old" not in brief


def test_export_brief_carries_no_token_url_key_or_configured_value(monkeypatch):
    (_home() / "config.yaml").write_text(
        "providers:\n"
        "- {name: p, kind: openai, base_url: 'http://x/v1', api_key_env: BUGREAD_PROVIDER_VAR, models: {default: m}}\n"
    )
    monkeypatch.setenv("BUGREAD_PROVIDER_VAR", "plainword-horse-battery")
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # 40 chars, a GitHub token shape
    url_key = "zq81mmvq"
    leak = f"auth failed with plainword-horse-battery and {token} at https://h.example/v1?api_key={url_key}"
    _write(_row("3" * 16, time.time() - 60, leak, traceback=f"RuntimeError: {leak}\n"))
    brief = bugread.export().read_text(encoding="utf-8")
    assert "## 1." in brief
    assert token not in brief
    assert url_key not in brief
    assert "plainword-horse-battery" not in brief


def test_crash_log_uncaught_exception_becomes_a_group_and_spawned_is_ignored():
    log = _home() / "logs" / "tui_gateway_crash.log"
    log.parent.mkdir(parents=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    log.write_text(
        f"[tui-parent] {stamp} [lifecycle] spawned gateway child pid=41 cmd=python cwd=/tmp\n"
        f"[tui-parent] {stamp} uncaughtException: TypeError: x is undefined\n"
        f"[tui-parent] {stamp} [lifecycle] child exit pid=41 code=0 signal=null\n"
        "=== gateway exit ===\n"
    )
    (g,) = bugread.groups()
    assert (g.kind, g.count) == ("tui_crash", 1)
    assert g.summary == "uncaughtException: TypeError: x is undefined"


def test_crash_log_nonzero_gateway_exit_is_a_crash():
    text = "[tui-parent] 2026-11-23T10:00:00.000Z [lifecycle] child exit pid=7 code=1 signal=null\n"
    (row,) = bugread.crash_rows(text, [])
    assert row["exc_type"] == "gateway_exit"
    assert row["ts"] == datetime(2026, 11, 23, 10, tzinfo=UTC).timestamp()
