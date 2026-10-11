"""Audit fixes: sub-agents cancelled before they start, custom agent tools, side calls priced by their own model."""

from __future__ import annotations

import asyncio
import logging

from k3code.providers.types import Message, StreamEvent, Usage
from k3code.router import Router, build_chain
from k3code.routing.tiers import TaskKind, Tier, TierRouters
from k3code.subagents import runner as runner_mod
from k3code.subagents.runner import Handle
from k3code.subagents.types import load_agent_types, parse_agent_md
from m1cmd_helpers import git_repo, make_server, new_session
from test_autonomy_gateway import events

# ── M1: interrupted before the first step ──


async def test_a_child_interrupted_before_its_first_step_is_finished_not_left_queued(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=["never asked"])
    sid = await new_session(server, repo)
    live = server.live_for(server.store.get(sid))
    mgr = server.subagents
    h = mgr.spawn(live, description="phantom", prompt="do it")
    assert h.status == "queued"
    clients = [*server.clients, server._stdio_client]

    def attach(to):
        for c in clients:
            c.session_id = to  # no client on the session: only the child can keep it

    attach(None)
    assert (await server.close_live(sid, disposable_only=True))["reason"] == "not disposable"
    attach(sid)  # the events below reach the session's clients
    assert mgr.interrupt(h.id)  # no await since spawn: the task has not taken its first step
    await asyncio.wait_for(mgr.wait(h), 5)
    assert provider.n == 0
    assert h.status == "interrupted" and h.done and h.finished_at > 0 and h.task is None
    (done,) = [e for e in _frames(server, "subagent.complete") if e["subagent_id"] == h.id]
    assert done["status"] == "interrupted"
    assert "interrupted" in h.render() and "no text" not in h.render()
    assert not [x for x in mgr.for_session(sid) if not x.done]  # the session may be closed again
    attach(None)
    assert await server.close_live(sid, disposable_only=True) == {"closed": True}


def _frames(server, kind):
    import json

    out = []
    for line in server._frames:
        f = json.loads(line)
        if f.get("method") == "event" and f["params"]["type"] == kind:
            out.append(f["params"]["payload"])
    return out


async def test_an_unstarted_cancelled_child_is_pruned_like_any_finished_one(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "KEEP_FINISHED_HANDLES", 0)
    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    sid = await new_session(server, repo)
    live = server.live_for(server.store.get(sid))
    h = server.subagents.spawn(live, description="phantom", prompt="do it")
    server.subagents.interrupt(h.id)
    await asyncio.wait_for(server.subagents.wait(h), 5)
    assert h.id not in server.subagents.handles


def test_a_handle_that_has_not_finished_does_not_render_as_an_empty_answer():
    h = Handle(id="sa-1", description="d", agent_type="worker", tier="main", depth=1, parent_sid="s")
    assert "still queued" in h.render()
    h.status = "running"
    assert "still running" in h.render()
    h.status = "completed"
    assert "no text" in h.render()


# ── M9: a custom agent's tools ──

AGENT = "---\nname: auditor\ndescription: reads things: carefully\n{tools}\n---\nYou audit.\n"


def test_tools_given_as_yaml_lines_are_read_not_dropped_to_all_tools():
    a = parse_agent_md(AGENT.format(tools="tools:\n  - read\n  - 'grep'\n  - glob"), "auditor")
    assert a is not None and a.tools == ["read", "grep", "glob"] and a.description == "reads things: carefully"


def test_inline_tools_still_parse():
    assert parse_agent_md(AGENT.format(tools="tools: read, grep"), "a").tools == ["read", "grep"]
    assert parse_agent_md(AGENT.format(tools='tools: ["read", "glob"]'), "a").tools == ["read", "glob"]
    assert parse_agent_md(AGENT.format(tools="tier: cheap"), "a").tools == []  # no key: all tools, as documented


def test_a_tools_key_that_names_nothing_skips_the_agent(caplog):
    for bad in ("tools:", "tools: []", "tools: ,"):
        with caplog.at_level(logging.WARNING):
            assert parse_agent_md(AGENT.format(tools=bad), "auditor") is None
    assert "names no tool" in caplog.text


def test_frontmatter_without_its_closing_delimiter_skips_the_agent(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_agent_md("---\nname: x\ntools: read\nYou do things.\n", "x") is None
    assert "no closing" in caplog.text
    assert parse_agent_md("You do things.\n", "plain").tools == []  # no frontmatter at all is fine


def test_a_broken_user_agent_leaves_the_builtin_of_that_name_in_force(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "explorer.md").write_text("---\nname: explorer\ntools:\n---\nbody\n")
    types = load_agent_types(home=tmp_path)
    assert types["explorer"].source == "builtin" and "write" not in types["explorer"].tools


# ── M2: a side call is recorded as the model that answered IT ──


class _Slow:
    base_url = "fake://"

    def __init__(self, name: str, delay: float) -> None:
        self.name, self.delay = name, delay

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        await asyncio.sleep(self.delay)
        yield StreamEvent(
            type="done", message=Message(role="assistant", content=model, usage=Usage(5, 2)), usage=Usage(5, 2)
        )

    async def aclose(self):
        return None


async def test_overlapping_side_calls_are_recorded_with_their_own_model_and_provider(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    cheap = Router(
        build_chain([_Slow("p-cheap", 0.15)], [["m-cheap"]]),
        max_retries=0,
        on_event=server._on_router_event,
        tier="cheap",
    )
    strong = Router(
        build_chain([_Slow("p-strong", 0.0)], [["m-strong"]]),
        max_retries=0,
        on_event=server._on_router_event,
        tier="strong",
    )
    tiers = TierRouters([], server.config, cooldowns=server.cooldowns)
    tiers._routers = {Tier.CHEAP: cheap, Tier.STRONG: strong}
    server._tiers = tiers
    msgs = [Message(role="user", content="hi")]
    title, review = await asyncio.gather(
        server.model_caller.complete(TaskKind.TITLE, msgs, session_id="s1"),
        server.model_caller.complete(TaskKind.REVIEW, msgs, session_id="s2"),
    )
    assert (title.tier, title.model, title.provider) == (Tier.CHEAP, "m-cheap", "p-cheap")
    assert (review.tier, review.model, review.provider) == (Tier.STRONG, "m-strong", "p-strong")
    rows = {r["session"]: r for r in server.usage.rows(0)}
    assert rows["s1"]["model"] == "m-cheap" and rows["s1"]["provider"] == "p-cheap"
    assert rows["s2"]["model"] == "m-strong"


async def test_a_sub_agent_records_the_model_on_its_own_answer_not_the_gateways_last_attempt(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    server, provider = make_server(tmp_path, monkeypatch, replies=["child done"])
    real_stream = provider.stream

    async def stream(messages, tools, model, **kw):
        server.last_attempt = ("p-other", "m-of-another-call")  # another session's call attempted meanwhile
        async for ev in real_stream(messages, tools, model, **kw):
            yield ev

    provider.stream = stream  # type: ignore[method-assign]
    sid = await new_session(server, repo)
    live = server.live_for(server.store.get(sid))
    h = await server.subagents.run(live, description="d", prompt="p")
    assert h.status == "completed", h.error
    assert h.model == "m"
    assert [e["model"] for e in events(server, "subagent.complete")][-1] == "m"
