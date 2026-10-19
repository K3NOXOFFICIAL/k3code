"""The system prompt stays one byte-identical prefix for a session while its volatile inputs change underneath it.

Providers cache tools -> system -> messages from the top, so any change to the system prompt misses the whole cached
conversation. Besides the learned tail (``test_prompt_stability``), three sections were rebuilt from live state for
every loop: the skills index (a fresh disk scan, ranked against the wall clock above the cap), the MCP section (live
connection order) and a custom output-style file (re-read every turn). The session now snapshots them at its first
prompt and reads them again only on compaction or on the user's own command that changes them (/output-style,
/reload-skills, /mcp reload, /reload). The orderings a snapshot is taken from are deterministic as well."""

from __future__ import annotations

import random
import time
from pathlib import Path

from k3code import mcpjson
from k3code import skills as skills_mod
from k3code.autonomy.proposals import Proposal
from k3code.config import McpServerConfig, Settings
from k3code.extratools import mcp_prompt, register_mcp_tools
from k3code.learning.curator import record_use
from k3code.mcpclient import McpManager, McpServerState, McpToolInfo
from k3code.paths import home
from k3code.prompting import build_system_prompt
from k3code.routing.tiers import TaskKind
from k3code.tools import ToolRegistry
from learn_helpers import FakeCaller
from m1cmd_helpers import cmd, make_server, new_session, rpc

T0 = 1_900_000_000.0  # a fixed "now" for the curator's usage records
DAY = 86400


class FakeMcp(McpManager):
    """An MCP manager whose connected tools the test sets; ``reload`` adopts ``after_reload``."""

    def __init__(self, tools: list[McpToolInfo]) -> None:
        super().__init__({})
        self.fake = list(tools)
        self.after_reload: list[McpToolInfo] | None = None

    def tools(self) -> list[McpToolInfo]:
        return list(self.fake)

    async def ensure_started(self) -> None:
        return None

    async def reload(self, servers=None) -> None:
        if self.after_reload is not None:
            self.fake = list(self.after_reload)


def tool(server: str, name: str) -> McpToolInfo:
    return McpToolInfo(server=server, name=name, description=f"{name} of {server}", schema={})


def write_skill(name: str, description: str) -> Path:
    path = home() / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody\n", encoding="utf-8")
    return path


def style_file(text: str) -> None:
    path = home() / "output-styles" / "mine.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def prompt_of(server, live) -> str:
    """The system prompt the session's next loop sends (each turn and each tier attempt builds one)."""
    return server._build_loop(live, None, server.router, TaskKind.INTERACTIVE_TURN, None).system_prompt


def mcp_part(prompt: str) -> str:
    return prompt.split("## MCP tools (deferred)", 1)[1].split("\n\n## ", 1)[0]


async def started(tmp_path: Path, monkeypatch):
    """A session over three user skills (cap 2, `c` used recently), two MCP servers and the custom style `mine`."""
    server, _ = make_server(tmp_path, monkeypatch, context={"skill_prompt_limit": 2})
    server.mcp = FakeMcp([tool("beta", "run"), tool("alpha", "read")])
    for name in ("a", "b", "c"):
        write_skill(name, f"skill {name}")
    record_use("c", clock=lambda: T0)
    monkeypatch.setattr(time, "time", lambda: T0 + DAY)  # rank_skills reads the clock through the time module
    style_file("Output style: mine, first version.")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sid = await new_session(server, repo)
    await cmd(server, "/output-style mine", sid)
    return server, server.live[sid]


async def test_volatile_sections_stay_frozen_until_the_users_own_commands_refresh_them(tmp_path, monkeypatch):
    server, live = await started(tmp_path, monkeypatch)
    try:
        first = prompt_of(server, live)
        assert "- a: skill a" in first and "- c: skill c" in first and "- b: skill b" not in first  # c: recent use
        assert "…and 1 more" in first
        assert "Output style: mine, first version." in first
        assert "mcp__alpha__read" in first

        # (a) a skill edited in place and one added
        write_skill("a", "skill a, rewritten")
        write_skill("d", "skill d")
        assert prompt_of(server, live) == first
        # (b) the servers reconnect in another order, and a new one connects mid-session
        server.mcp.fake = [tool("alpha", "read"), tool("beta", "run")]
        assert prompt_of(server, live) == first
        server.mcp.fake.append(tool("gamma", "late"))
        late = prompt_of(server, live)
        assert late == first and "gamma" not in late
        # (c) the custom style file edited
        style_file("Output style: mine, second version.")
        assert prompt_of(server, live) == first
        # (d) the clock moves past the 30-day cutoff of `c`'s last use
        monkeypatch.setattr(time, "time", lambda: T0 + 40 * DAY)
        for _ in range(3):  # every later turn and tier attempt
            assert prompt_of(server, live) == first

        # /output-style re-reads the style file, even under the same name
        await cmd(server, "/output-style mine", live.session_id)
        styled = prompt_of(server, live)
        assert "Output style: mine, second version." in styled and "first version" not in styled
        assert "- c: skill c" in styled and "gamma" not in styled  # the other sections keep their snapshot

        # /reload-skills scans and ranks the skills again, against the clock of now
        assert "result" in await rpc(server, "skills.reload", {"session_id": live.session_id})
        skilled = prompt_of(server, live)
        assert "- a: skill a, rewritten" in skilled and "- b: skill b" in skilled and "- c: skill c" not in skilled
        assert "…and 2 more" in skilled and "gamma" not in skilled

        # /mcp reload lists the servers it restarted, sorted by server then tool
        server.mcp.after_reload = [tool("zeta", "b"), tool("alpha", "z"), tool("alpha", "a")]
        await cmd(server, "/mcp reload", live.session_id)
        reloaded = prompt_of(server, live)
        assert "mcp__alpha__a, mcp__alpha__z, mcp__zeta__b" in mcp_part(reloaded)
        assert prompt_of(server, live) == reloaded  # and it is the new stable prefix

        # compaction rebuilds the cache anyway: everything is read again
        style_file("Output style: mine, third version.")
        server.mcp.fake = [tool("omega", "x")]
        assert prompt_of(server, live) == reloaded
        live.stored.messages.extend(
            m for i in range(40) for m in ({"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": "a"})
        )
        server.model_caller = FakeCaller("the work so far")  # type: ignore[assignment]
        assert await server._maybe_compact(live, force=True)
        compacted = prompt_of(server, live)
        assert "third version" in compacted and "mcp__omega__x" in compacted
        assert prompt_of(server, live) == compacted
    finally:
        await server.close()


async def test_reload_reads_style_skills_and_mcp_again(tmp_path, monkeypatch):
    server, live = await started(tmp_path, monkeypatch)
    try:
        first = prompt_of(server, live)
        style_file("Output style: mine, second version.")
        write_skill("d", "skill d")
        server.mcp.fake = [tool("gamma", "late")]
        assert prompt_of(server, live) == first
        assert "result" in await rpc(server, "reload.env", {"session_id": live.session_id})
        after = prompt_of(server, live)
        assert "second version" in after and "…and 2 more" in after and "mcp__gamma__late" in after
    finally:
        await server.close()


async def test_a_late_mcp_server_is_reachable_through_mcp_tool_search_and_named_after_a_refresh(tmp_path, monkeypatch):
    server, live = await started(tmp_path, monkeypatch)
    try:
        first = prompt_of(server, live)
        server.mcp.fake.append(tool("gamma", "late"))
        loop = server._build_loop(live, None, server.router, TaskKind.INTERACTIVE_TURN, None)
        assert loop.system_prompt == first and "gamma" not in first
        found = await loop.tools._tools["mcp_tool_search"][1]({"query": "mcp__gamma__late"})  # noqa: SLF001
        assert "mcp__gamma__late" in found["content"]
        assert "mcp__gamma__late" in loop.tools._tools  # noqa: SLF001  # registered (deferred) and callable
    finally:
        await server.close()


async def test_the_compaction_size_check_before_the_first_turn_does_not_freeze_an_empty_mcp_section(
    tmp_path, monkeypatch
):
    # the first turn after a daemon start checks the request size before the MCP servers are started
    server, live = await started(tmp_path, monkeypatch)
    try:
        server.mcp.fake = []
        assert server._request_tokens(live) > 0
        server.mcp.fake = [tool("alpha", "read")]
        assert "mcp__alpha__read" in prompt_of(server, live)
    finally:
        await server.close()


# ── determinism of what a snapshot is taken from ──


def _shuffled(items: list, seed: int) -> list:
    out = list(items)
    random.Random(seed).shuffle(out)
    return out


def test_the_mcp_section_is_sorted_by_server_then_tool_whatever_the_connection_order():
    tools = [tool("zeta", "b"), tool("alpha", "z"), tool("mid", "q"), tool("alpha", "a"), tool("zeta", "a")]
    texts = {mcp_prompt(FakeMcp(_shuffled(tools, seed))) for seed in range(6)}
    assert len(texts) == 1
    assert texts.pop().endswith("mcp__alpha__a, mcp__alpha__z, mcp__mid__q, mcp__zeta__a, mcp__zeta__b")


def test_the_capped_skill_list_depends_on_its_inputs_only(tmp_path, monkeypatch):
    found = [skills_mod.Skill(n, f"skill {n}", tmp_path / n / "SKILL.md") for n in "abcdef"]
    record_use("e", clock=lambda: T0)
    order = iter(range(100))
    monkeypatch.setattr(skills_mod, "discover", lambda cwd, roots=None: _shuffled(found, next(order)))
    monkeypatch.setattr(time, "time", lambda: T0 + DAY)
    capped = skills_mod.skills_prompt(tmp_path, limit=3, now=T0 + DAY)
    assert "- e: skill e" in capped and "…and 3 more" in capped
    monkeypatch.setattr(time, "time", lambda: T0 + 40 * DAY)  # the wall clock is not an input: `now` is
    assert skills_mod.skills_prompt(tmp_path, limit=3, now=T0 + DAY) == capped
    full = {skills_mod.skills_prompt(tmp_path, limit=30) for _ in range(4)}  # under the cap: listed by name
    assert len(full) == 1


def test_two_fresh_snapshots_of_the_same_inputs_are_identical(tmp_path, monkeypatch):
    for name in "abc":
        write_skill(name, f"skill {name}")
    tools = [tool("zeta", "b"), tool("alpha", "z"), tool("alpha", "a")]
    real = skills_mod.discover
    seeds = iter(range(100))
    monkeypatch.setattr(skills_mod, "discover", lambda cwd, roots=None: _shuffled(real(cwd, roots), next(seeds)))
    config = Settings(context={"skill_prompt_limit": 2})
    one = build_system_prompt("BASE", cwd=tmp_path, config=config, mcp=FakeMcp(tools), frozen={})
    two = build_system_prompt("BASE", cwd=tmp_path, config=config, mcp=FakeMcp(tools[::-1]), frozen={})
    assert one == two


# ── what the user changes through other doors than the slash commands ──


async def two_sessions(tmp_path, monkeypatch):
    server, live = await started(tmp_path, monkeypatch)
    repo2 = tmp_path / "repo2"
    (repo2 / ".git").mkdir(parents=True)
    return server, live, server.live[await new_session(server, repo2)]


async def test_mcp_reload_refreshes_every_live_session(tmp_path, monkeypatch):
    server, live, other = await two_sessions(tmp_path, monkeypatch)
    try:
        assert "mcp__alpha__read" in prompt_of(server, live) and "mcp__alpha__read" in prompt_of(server, other)
        server.mcp.after_reload = [tool("zeta", "b")]
        await cmd(server, "/mcp reload", live.session_id)
        assert "mcp__zeta__b" in prompt_of(server, live)
        assert "mcp__zeta__b" in prompt_of(server, other)
    finally:
        await server.close()


async def test_an_accepted_skill_card_refreshes_the_skills_of_every_session(tmp_path, monkeypatch):
    server, live, other = await two_sessions(tmp_path, monkeypatch)
    try:
        assert "- a: skill a" in prompt_of(server, other)
        card = Proposal(
            id="s1", kind="skill", text="archive a", action="", key="s1", payload=dict(op="archive", drop="a")
        )
        assert "archived" in await server.learning.apply(card, live)
        for s in (live, other):
            prompt = prompt_of(server, s)
            assert "- a: skill a" not in prompt and "- b: skill b" in prompt  # the next skill fills the cap
    finally:
        await server.close()


async def test_an_accepted_mcp_card_refreshes_the_mcp_section_of_every_session(tmp_path, monkeypatch):
    server, live, other = await two_sessions(tmp_path, monkeypatch)
    try:
        assert "mcp__docs__q" not in prompt_of(server, other)
        server.mcp.after_reload = [tool("docs", "q")]
        payload = dict(op="recipe", type="mcp", root=str(live.perms.cwd), name="docs", server={"command": "true"})
        card = Proposal(id="m1", kind="mcp", text="add docs", action="", key="m1", payload=payload)
        await server.learning.apply(card, live)
        for s in (live, other):
            assert "mcp__docs__q" in prompt_of(server, s)
    finally:
        await server.close()


async def test_a_change_of_the_mcp_servers_through_config_refreshes_the_prompt(tmp_path, monkeypatch):
    server, live, other = await two_sessions(tmp_path, monkeypatch)
    try:
        first = prompt_of(server, live)
        server.mcp.fake = [tool("gamma", "late")]
        server.apply_file_config(live.perms.cwd)  # the servers did not change: the snapshot stays
        assert prompt_of(server, live) == first
        servers = {"gamma": McpServerConfig(command="true")}
        monkeypatch.setattr(mcpjson, "merged", lambda *_a, **_k: servers)  # e.g. /config set mcp.servers.gamma
        server.apply_file_config(live.perms.cwd)
        for s in (live, other):
            assert "mcp__gamma__late" in prompt_of(server, s)
    finally:
        await server.close()


async def test_an_empty_first_mcp_snapshot_is_not_kept_once_a_server_connects(tmp_path, monkeypatch):
    server, live = await started(tmp_path, monkeypatch)
    try:
        server.mcp.fake = []  # every server down when the session's first loop is built
        assert "## MCP tools" not in prompt_of(server, live)
        server.mcp.fake = [tool("alpha", "read")]
        named = prompt_of(server, live)
        assert "mcp__alpha__read" in named
        server.mcp.fake = [tool("alpha", "read"), tool("beta", "run")]
        assert prompt_of(server, live) == named  # and now it is the snapshot
    finally:
        await server.close()


class _Runner:
    def __init__(self, status: str, error: str = "") -> None:
        self.state = McpServerState(name="alpha", transport="stdio", status=status, error=error)


async def test_a_tool_of_a_crashed_then_restarted_server_is_reported_with_the_servers_state():
    mcp = McpManager({})
    mcp._runners["alpha"] = _Runner("failed", "connect timeout")  # type: ignore[assignment]  # restarted: no tools
    assert await mcp.call("mcp__alpha__read", {}) == {"error": "MCP server alpha is failed: connect timeout"}
    assert (await mcp.call("mcp__nope__x", {}))["error"] == "Unknown MCP tool: mcp__nope__x"

    other = _Runner("connected")
    other.state.name, other.state.tools = "beta", [tool("beta", "run")]
    mcp._runners["beta"] = other  # type: ignore[assignment]
    reg = ToolRegistry()
    register_mcp_tools(reg, mcp)
    found = await reg._tools["mcp_tool_search"][1]({"query": "read"})  # noqa: SLF001
    assert found["content"].startswith("No matching MCP tools.")
    assert "alpha (failed: connect timeout)" in found["content"]


async def test_the_compaction_size_check_does_not_freeze_a_partial_mcp_section(tmp_path, monkeypatch):
    # one server is up when the size check runs before the first turn, the other connects before the first loop
    server, live = await started(tmp_path, monkeypatch)
    try:
        server.mcp.fake = [tool("alpha", "read")]
        assert server._request_tokens(live) > 0
        server.mcp.fake = [tool("alpha", "read"), tool("beta", "run")]
        assert "mcp__beta__run" in prompt_of(server, live)
    finally:
        await server.close()
