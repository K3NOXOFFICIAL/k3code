"""A usage.db write error (locked, disk full, read-only, closed) must not fail a turn or abort a failover (#148)."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from k3code import usage as usage_mod
from k3code.config import ProviderEntry, Settings
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.providers.openai_compat import OpenAICompatProvider
from k3code.providers.types import Message
from k3code.router import Router, build_chain
from k3code.usage import UsageDB


class _FailingConn:
    """Stands in for the sqlite connection: every write raises, like a locked or full database."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def execute(self, *a, **k):
        self.calls += 1
        raise self.exc

    def commit(self):
        raise self.exc

    def rollback(self):
        pass

    def close(self):
        pass


def _provider(responses):
    client = AsyncMock(spec=httpx.AsyncClient)
    client.send = AsyncMock(side_effect=responses)
    client.build_request = MagicMock(return_value=MagicMock())
    client.aclose = AsyncMock()
    return OpenAICompatProvider(name="p", base_url="https://api.test.com", api_key="k", client=client)


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database or disk is full"),
        OSError(28, "ENOSPC"),
    ],
)
def test_record_swallows_write_errors(tmp_path: Path, exc: Exception):
    db = UsageDB(tmp_path / "usage.db")
    db._db = _FailingConn(exc)
    db.record("call", session="s", tokens_in=1)  # does not raise


def test_record_on_a_closed_database_does_not_raise(tmp_path: Path):
    db = UsageDB(tmp_path / "usage.db")
    db._db.close()
    db.record("failover", session="s")


def test_record_still_rejects_an_unknown_kind(tmp_path: Path):
    db = UsageDB(tmp_path / "usage.db")
    with pytest.raises(ValueError):
        db.record("nonsense")


def test_record_still_writes_when_the_database_works(tmp_path: Path):
    db = UsageDB(tmp_path / "usage.db")
    db.record("call", session="s", tokens_in=3, tokens_out=4)
    (row,) = db.rows()
    assert (row["kind"], row["tokens_in"], row["tokens_out"]) == ("call", 3, 4)


def test_write_failures_are_logged_once_a_minute(tmp_path: Path, monkeypatch, caplog):
    db = UsageDB(tmp_path / "usage.db")
    db._db = _FailingConn(sqlite3.OperationalError("database is locked"))
    clock = [1000.0]
    monkeypatch.setattr(usage_mod.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger="k3code.usage"):
        for _ in range(5):
            db.record("call")
        assert len(caplog.records) == 1
        assert "database is locked" in caplog.records[0].getMessage()
        clock[0] += 61
        db.record("call")
    assert len(caplog.records) == 2
    assert all(r.levelno == logging.WARNING for r in caplog.records)


async def test_failover_survives_a_failing_usage_write(tmp_path: Path, monkeypatch):
    """The gateway's router callback records the failover; a failing write used to abort the walk."""
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    srv = GatewayServer(
        config=Settings(providers=[prov], default_model="default", permission_mode="yolo"),
        store=SessionStore(tmp_path / "s.db"),
    )
    srv._write = lambda s: None
    srv.usage._db = _FailingConn(sqlite3.OperationalError("database is locked"))

    primary = _provider([httpx.Response(401, json={"error": {"message": "Invalid API key"}})])
    ok = 'data: {"choices":[{"delta":{"content":"OK"}}]}\ndata: {"choices":[{"delta":{}}]}\ndata: [DONE]\n'
    backup_client_responses = [httpx.Response(200, text=ok, headers={"content-type": "text/event-stream"})]
    backup = _provider(backup_client_responses)
    router = Router(
        build_chain([primary, backup], [["m1"], ["m2"]]),
        max_retries=2,
        base_delay=0.01,
        max_delay=0.05,
        on_event=srv._on_router_event,
    )

    result = await router.complete([Message(role="user", content="hi")], [])

    assert result.content == "OK"
    assert srv.usage._db.calls >= 1  # the failover row was attempted and failed


async def test_a_turn_completes_while_usage_writes_fail(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    script = tmp_path / "s.json"
    script.write_text(json.dumps([{"type": "text", "text": "done"}]))
    monkeypatch.setenv("K3CODE_FAKE_PROVIDER", str(script))
    prov = ProviderEntry(name="t", kind="openai", base_url="http://t", api_key_env="NOPE", models={"default": "m"})
    srv = GatewayServer(
        config=Settings(providers=[prov], default_model="default", permission_mode="yolo"),
        store=SessionStore(tmp_path / "s.db"),
    )
    srv._write = lambda s: None
    srv.usage._db = _FailingConn(sqlite3.OperationalError("database or disk is full"))
    project = tmp_path / "proj"
    project.mkdir()
    live = srv.live_for(srv.store.create(title="t", model="default", cwd=str(project)))

    status, text = await srv._run_turn(live, "hello")

    assert status == "done", (text, live.last_error)
    assert srv.usage._db.calls >= 1
