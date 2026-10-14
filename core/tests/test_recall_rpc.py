"""`session.search`: the recall archive over JSON-RPC for clients (read-only, needs the daemon token)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from k3code import recall
from k3code.gateway.sessions import SessionStore
from k3code.integrations.panes import readonly_verdict
from k3code.memory import identity_root
from m1cmd_helpers import make_server, rpc

DAY = 86400.0
GHP = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
INVALID = -32602


@pytest.fixture
def projects(tmp_path):
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    assert identity_root(a) == a and identity_root(b) == b  # no enclosing checkout: each dir is its own project
    return a, b


@pytest.fixture
async def server(tmp_path, monkeypatch):
    srv, _ = make_server(tmp_path / "srv", monkeypatch)
    yield srv
    await srv.close()


def add(store: SessionStore, cwd: Path | str, texts: list[str], *, age_days: float = 0.0, title: str = "") -> str:
    s = store.create(cwd=str(cwd), title=title)
    s.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(texts)]
    store.save(s)
    if age_days:
        store._db.execute(
            "UPDATE message_archive SET ts = ? WHERE session_id = ?", (time.time() - age_days * DAY, s.session_id)
        )
        store._db.commit()
    return s.session_id


async def search(server, **params) -> dict:
    reply = await rpc(server, "session.search", params)
    assert "result" in reply, reply
    return reply["result"]


async def test_results_have_the_documented_shape(server, projects):
    a, _ = projects
    sid = add(server.store, a, ["we chose postgres for the ledger", "fine"], title="db choice")
    res = await search(server, query="postgres ledger")
    assert res["indexing"] == 0
    assert len(res["results"]) == 1
    hit = res["results"][0]
    assert set(hit) == {"session_id", "title", "cwd", "at", "role", "seq", "snippet", "approximate"}
    assert (hit["session_id"], hit["title"], hit["cwd"]) == (sid, "db choice", str(a))
    assert (hit["role"], hit["seq"]) == ("user", 0)
    assert "postgres" in hit["snippet"] and hit["approximate"] is False
    assert abs(hit["at"] - time.time()) < 60


async def test_finds_a_four_month_old_fact_among_300_sessions(server, projects):
    a, b = projects
    for i in range(300):
        add(server.store, a if i % 2 else b, [f"routine chat number {i} about lint and tests", "done"], age_days=i % 30)
    old = add(server.store, b, ["the staging vaultwarden rotation runs nightly", "noted"], age_days=120, title="ops")
    res = await search(server, query="how does vaultwarden rotation work")
    assert [(r["session_id"], r["seq"]) for r in res["results"]] == [(old, 0)]
    assert res["results"][0]["at"] < time.time() - 119 * DAY


async def test_limit_caps_the_results(server, projects):
    for i in range(30):
        add(server.store, projects[0], [f"kafka partition note {i}"])
    assert len((await search(server, query="kafka partition"))["results"]) == 20  # the default
    assert len((await search(server, query="kafka partition", limit=3))["results"]) == 3
    assert len((await search(server, query="kafka partition", limit=50))["results"]) == 30


async def test_since_keeps_the_recent_hits(server, projects):
    a, _ = projects
    old = add(server.store, a, ["grafana dashboard for latency"], age_days=200)
    new = add(server.store, a, ["grafana dashboard moved"], age_days=3)
    both = {r["session_id"] for r in (await search(server, query="grafana dashboard"))["results"]}
    assert both == {old, new}
    for since in ("90d", time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * DAY))):
        res = await search(server, query="grafana dashboard", since=since)
        assert [r["session_id"] for r in res["results"]] == [new], since


async def test_cwd_ranks_its_own_project_first(server, projects):
    a, b = projects
    # the same words; alpha's is the older one, so recency alone would put beta's first
    in_a = add(server.store, a, ["redis eviction policy is allkeys-lru"], age_days=40)
    in_b = add(server.store, b, ["redis eviction policy is allkeys-lru"], age_days=1)
    plain = [r["session_id"] for r in (await search(server, query="redis eviction policy"))["results"]]
    assert plain == [in_b, in_a]
    boosted = [r["session_id"] for r in (await search(server, query="redis eviction policy", cwd=str(a)))["results"]]
    assert boosted == [in_a, in_b]  # not a filter: the other project's hit is still there
    by_project = await search(server, query="redis eviction policy", project=str(a))
    assert [r["session_id"] for r in by_project["results"]] == [in_a, in_b]


@pytest.mark.parametrize(
    ("params", "named"),
    [
        ({}, "query"),
        ({"query": ""}, "query"),
        ({"query": "   "}, "query"),
        ({"query": 7}, "query"),
        ({"query": ["a"]}, "query"),
        ({"query": "x", "limit": 0}, "limit"),
        ({"query": "x", "limit": 51}, "limit"),
        ({"query": "x", "limit": -3}, "limit"),
        ({"query": "x", "limit": "5"}, "limit"),
        ({"query": "x", "limit": 2.5}, "limit"),
        ({"query": "x", "limit": True}, "limit"),
        ({"query": "x", "since": "yesterday"}, "since"),
        ({"query": "x", "since": "99999999d"}, "since"),
        ({"query": "x", "since": 5}, "since"),
        ({"query": "x", "cwd": 5}, "cwd"),
        ({"query": "x", "cwd": ["/tmp"]}, "cwd"),
        ({"query": "x", "project": {"a": 1}}, "project"),
    ],
)
async def test_hostile_params_are_refused_with_the_field_named(server, params, named):
    reply = await rpc(server, "session.search", params)
    assert reply.get("error", {}).get("code") == INVALID, reply
    assert named in reply["error"]["message"], reply


async def test_a_secret_in_an_old_message_or_title_comes_out_masked(server, projects):
    a, _ = projects
    other = add(server.store, a, ["placeholder"])
    leaked = "deploy key " + GHP + " for the release bucket"
    # straight into the archive and the row, as an older pattern set would leave them: only the output scrub is left
    server.store._db.execute(
        "INSERT INTO message_archive (session_id, seq, role, ts, cwd, digest, text) VALUES (?,?,?,?,?,?,?)",
        (other, 5, "user", time.time() - 120 * DAY, str(a), "raw", leaked),
    )
    server.store._db.execute("UPDATE sessions SET title = ? WHERE session_id = ?", ("about " + GHP, other))
    server.store._db.commit()
    res = await search(server, query="release bucket deploy")
    assert [(r["session_id"], r["seq"]) for r in res["results"]] == [(other, 5)]
    assert GHP not in json.dumps(res)
    assert "[release] [bucket]" in res["results"][0]["snippet"] and res["results"][0]["title"].startswith("about ")


async def test_indexing_counts_the_sessions_not_archived_yet(server, projects):
    a, _ = projects
    server.store._db.execute(
        "INSERT INTO sessions (session_id, cwd, messages, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("old0000000000000", str(a), json.dumps([{"role": "user", "content": "x"}]), 1.0, 1.0),
    )
    server.store._db.commit()
    add(server.store, a, ["cassandra compaction"])
    assert (await search(server, query="cassandra compaction"))["indexing"] == 1
    assert (await search(server, query="nothing matches this"))["indexing"] == 1


async def test_the_search_runs_off_the_event_loop_thread(server, projects, monkeypatch):
    add(server.store, projects[0], ["prometheus alert"])
    seen: list[threading.Thread] = []
    real = recall.search_file

    def spy(*args, **kwargs):
        seen.append(threading.current_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(recall, "search_file", spy)
    res = await search(server, query="prometheus")
    assert len(res["results"]) == 1
    assert len(seen) == 1 and seen[0] is not threading.current_thread()


def test_a_read_only_pane_may_search_and_a_stranger_may_not():
    from k3code.gateway import server as gateway_server

    assert "session.search" in gateway_server._HANDLERS
    assert readonly_verdict({"jsonrpc": "2.0", "id": 1, "method": "session.search", "params": {}}) is None
    assert "session.search" not in gateway_server.OPEN_METHODS
