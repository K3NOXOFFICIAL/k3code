"""M1 efficiency: one token definition across providers, per-turn totals, tokens in /stats and collect()."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import httpx
import pytest

from k3code.learning import optimizer
from k3code.learning.decisions import DecisionLog
from k3code.providers.anthropic import AnthropicProvider
from k3code.providers.claude_cli import _usage
from k3code.providers.types import Message
from k3code.usage import KINDS, UsageDB, format_stats

SRC = Path(__file__).resolve().parents[1] / "src" / "k3code"

ANTHROPIC_BODY = (
    'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":3,'
    '"cache_read_input_tokens":100,"cache_creation_input_tokens":50,"output_tokens":1}}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta",'
    '"text":"Hi"}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


async def test_anthropic_and_claude_cli_count_the_same_turn_equally():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=ANTHROPIC_BODY.encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(name="a", base_url="https://a.test", api_key="k", client=client)
    events = [e async for e in provider.stream([Message(role="user", content="hi")], [], "m")]
    api = events[-1].usage
    cli = _usage({"usage": {"input_tokens": 3, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50,
                            "output_tokens": 7}})
    assert (api.prompt_tokens, api.completion_tokens) == (cli.prompt_tokens, cli.completion_tokens) == (153, 7)


def test_per_turn_totals_add_up_to_the_session_total(tmp_path):
    db = UsageDB(tmp_path / "usage.db")
    for turn, tin, tout in (("t1", 100, 10), ("t1", 120, 12), ("t2", 300, 30)):
        db.record("call", session="s1", tokens_in=tin, tokens_out=tout, tier="main", task_kind="interactive_turn",
                  turn=turn)
    db.record("call", session="s1", tokens_in=5, tokens_out=1, tier="cheap", task_kind="title")  # side call, no turn
    (session,) = db.aggregate("session")
    turns = {g["key"]: g for g in db.aggregate("turn")}
    assert turns["t1"]["tokens_in"] == 220 and turns["t2"]["tokens_out"] == 30
    assert sum(g["tokens_in"] for g in turns.values()) == session["tokens_in"] == 525
    assert sum(g["tokens_out"] for g in turns.values()) == session["tokens_out"] == 53
    assert session["tokens_by_kind"]["interactive_turn"] == {"calls": 3, "tokens_in": 520, "tokens_out": 52}
    assert session["by_tier"]["cheap"]["tokens_in"] == 5


def test_stats_and_collect_report_tokens_per_tier_kind_and_turn(tmp_path):
    db = UsageDB(tmp_path / "usage.db")
    db.record("call", session="s1", tokens_in=1000, tokens_out=100, tier="main", task_kind="interactive_turn",
              turn="t1")
    db.record("call", session="s1", tokens_in=200, tokens_out=20, tier="cheap", task_kind="title", turn="t2")
    text = format_stats(db.aggregate("session"), "session")
    assert "1000/100" in text and "kinds: interactive_turn 1 calls (1000/100 tok)" in text
    m = optimizer.collect(db.rows(0), DecisionLog(tmp_path), [], since=0)
    assert m["tokens"] == 1320 and m["turns"] == 2 and m["tokens_per_turn"] == 660.0
    assert m["tokens_by_tier"] == {"main": 1100, "cheap": 220}
    assert m["tokens_by_kind"] == {"interactive_turn": 1100, "title": 220}


def _usage_record_kind(node: ast.AST) -> str | None:
    """The literal kind of a ``<...>.usage.record("kind", ...)`` call, else None."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "record"):
        return None
    target = node.func.value
    name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
    if name != "usage" or not node.args or not isinstance(node.args[0], ast.Constant):
        return None
    return node.args[0].value


def test_record_rejects_unknown_kinds_and_every_written_kind_is_listed(tmp_path):
    db = UsageDB(tmp_path / "usage.db")
    with pytest.raises(ValueError):
        db.record("made_up_kind")
    written = {
        kind
        for path in SRC.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if (kind := _usage_record_kind(node)) is not None
    }
    assert written and written <= set(KINDS), written - set(KINDS)
    assert re.search(r"usage\.record\(", (SRC / "routing" / "caller.py").read_text())


async def test_gateway_call_rows_carry_the_turn_id(tmp_path, monkeypatch):
    from test_permissions_gateway import call, make_server, run_turn

    server, _ = make_server(tmp_path, ["one", "two"], monkeypatch)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await run_turn(server, "first task", [])
    await run_turn(server, "second task", [])
    rows = [r for r in server.usage.rows(0) if r["kind"] == "call"]
    turns = {r["turn"] for r in rows}
    assert len(turns) == 2 and "" not in turns
    assert {g["key"] for g in server.usage.aggregate("turn")} == turns
