"""Twelfth pass: prompt-cache prefix breaks that nothing explained. The provider caches the tool list, the system prompt
and the conversation from the top, so each of these rewrote the whole cached conversation for no reason."""

from __future__ import annotations

from k3code.automation.runner import TickContext
from k3code.automation.server_runner import ServerRunner
from k3code.tools import ToolRegistry
from m1cmd_helpers import make_server, new_session, submit_and_wait


def _record_tools(provider) -> list[list[str]]:
    """The tool names of every request that offers tools (side calls without tools are left out)."""
    seen: list[list[str]] = []
    original = provider.stream

    async def stream(messages, tools, model, **kwargs):
        if tools:
            seen.append([t.name for t in tools])
        async for event in original(messages, tools, model, **kwargs):
            yield event

    provider.stream = stream
    return seen


async def test_a_session_with_a_self_paced_loop_offers_schedule_next_on_every_turn(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch, ["ok"])
    offered = _record_tools(provider)
    sid = await new_session(server, tmp_path)
    runner = ServerRunner(server)
    await submit_and_wait(server, "user one")
    await runner.run_prompt("tick 1", session_id=sid, kind="loop_tick", tick=TickContext())
    await submit_and_wait(server, "user two")
    await runner.run_prompt("tick 2", session_id=sid, kind="loop_tick", tick=TickContext())
    await submit_and_wait(server, "user three")
    await runner.run_prompt("a cron job", session_id=sid, kind="cron_job")  # no tick: the tool list stays as it is
    await submit_and_wait(server, "user four")
    after_the_loop_began = offered[1:]  # the list changes once, when the loop starts, and never back
    assert len(after_the_loop_began) >= 6
    assert len({tuple(names) for names in after_the_loop_began}) == 1, "the tool list changed between turns"
    assert after_the_loop_began[0][-1] == "schedule_next", "volatile tools come last, behind the rest"
    await server.close()


async def test_schedule_next_outside_a_tick_says_there_is_no_loop(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sid = await new_session(server, tmp_path)
    tick = TickContext()
    await ServerRunner(server).run_prompt("tick", session_id=sid, kind="loop_tick", tick=tick)
    (installer,) = server.live[sid].extra_tools
    reg = ToolRegistry()
    installer(reg)
    _, handler = reg.get("schedule_next")
    assert await handler({"seconds": 120}) == {"error": "no self-paced loop is active"}
    assert tick.next_seconds is None
    installer.tick = tick  # what run_prompt does for the duration of a tick
    assert (await handler({"seconds": 120}))["content"].startswith("Next tick in 120s")
    assert tick.next_seconds == 120
    await server.close()


async def test_the_models_own_edit_of_a_project_memory_file_does_not_change_the_sessions_prompt(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    (tmp_path / "AGENTS.md").write_text("# Agents\n- use uv\n")
    live = server.live[await new_session(server, tmp_path)]
    first = server._system_prompt_for(live)
    assert "- use uv" in first
    (tmp_path / "AGENTS.md").write_text("# Agents\n- use uv\n- NEW CONVENTION: tabs not spaces\n")  # the model's edit
    (tmp_path / "CLAUDE.md").write_text("a file a checkout brought in\n")
    (tmp_path / ".k3code" / "rules").mkdir(parents=True)
    (tmp_path / ".k3code" / "rules" / "style.md").write_text("a rule that appeared\n")
    assert server._system_prompt_for(live) == first, "byte-identical: the provider keeps the conversation cached"
    live.refresh_prompt_inputs()  # a compaction, /clear or a move to another project
    again = server._system_prompt_for(live)
    assert "tabs not spaces" in again and "a file a checkout brought in" in again and "a rule that appeared" in again
    await server.close()


async def test_a_project_memory_line_added_with_the_command_reaches_the_live_prompt(tmp_path, monkeypatch):
    from m1cmd_helpers import cmd

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    (tmp_path / "AGENTS.md").write_text("# Agents\n- use uv\n")
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    first = server._system_prompt_for(live)
    await cmd(server, "/memory add always run ruff", sid)
    assert "always run ruff" in (tmp_path / "AGENTS.md").read_text()
    assert "always run ruff" in server._system_prompt_for(live), "the user's own line takes effect now"
    assert server._system_prompt_for(live).startswith(first.split("## Project memory")[0])
    await server.close()


async def test_a_project_memory_file_another_directory_has_is_read_there(tmp_path, monkeypatch):
    from k3code.memory import memory_prompt

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    here, there = tmp_path / "here", tmp_path / "there"
    for d, line in ((here, "rule of here"), (there, "rule of there")):
        (d / ".git").mkdir(parents=True)
        (d / "AGENTS.md").write_text(f"- {line}\n")
    frozen: dict = {}
    assert "rule of here" in memory_prompt(here, frozen=frozen)
    # a copy of the session's snapshot used for a sub-agent in a worktree (another root) reads that tree's files
    other = memory_prompt(there, frozen=dict(frozen))
    assert "rule of there" in other and "rule of here" not in other


async def test_the_first_appearance_of_user_md_does_not_change_the_sessions_prompt(tmp_path, monkeypatch):
    from k3code import memory as memory_mod

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    live = server.live[await new_session(server, tmp_path)]
    user_file = memory_mod.user_memory_path()
    assert not user_file.exists()
    first = server._system_prompt_for(live)
    assert "## User memory" not in first
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("- I like short answers\n")  # the first distill, or the user, creates it
    assert server._system_prompt_for(live) == first, "absence is part of what the session first saw"
    live.refresh_prompt_inputs()
    assert "I like short answers" in server._system_prompt_for(live)
    await server.close()


def test_a_user_memory_with_nothing_learned_yet_is_not_a_section(tmp_path, monkeypatch):
    from k3code import memory as memory_mod
    from k3code.learning.distiller import write_auto_section

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    (tmp_path / ".git").mkdir()
    user_file = memory_mod.user_memory_path()
    user_file.parent.mkdir(parents=True)
    write_auto_section(user_file, [])  # what the first distill writes when it learned nothing
    assert "nothing learned yet" in user_file.read_text()
    assert "User memory" not in memory_mod.memory_prompt(tmp_path)
    assert "User memory" not in memory_mod.memory_prompt(tmp_path, frozen={})
    user_file.write_text("- my own line\n\n## Learned preferences (auto)\n- (nothing learned yet)\n")
    assert "my own line" in memory_mod.memory_prompt(tmp_path)  # only a section with nothing else in it is dropped


def _mcp_with_tools(*tools: str):
    from k3code.config import McpServerConfig
    from k3code.mcpclient import McpManager, McpToolInfo, _Runner

    manager = McpManager({"s": McpServerConfig(command="x")})
    runner = _Runner("s", manager.servers["s"])
    runner.state.tools = [McpToolInfo("s", t, "", {}) for t in tools]
    manager._runners["s"] = runner
    return manager


def test_an_mcp_server_that_connects_after_the_first_turn_does_not_change_the_prompt(tmp_path, monkeypatch):
    from k3code.config import Settings
    from k3code.prompting import build_system_prompt

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    manager = _mcp_with_tools()  # configured, started, and the server is down at the first turn
    manager._started = True
    frozen: dict = {}

    def build(mcp=manager) -> str:
        return build_system_prompt("base", cwd=tmp_path, config=Settings(), mcp=mcp, frozen=frozen)

    first = build()
    assert "MCP tools" not in first
    manager._runners["s"].state.tools = _mcp_with_tools("go")._runners["s"].state.tools  # it connects
    assert manager.tools() and build() == first, "mcp_tool_search finds the tool; the prompt keeps its bytes"
    frozen.clear()  # a compaction or /clear
    assert "mcp__s__{go}" in build()
    # a sub-agent has no MCP tools: the parent's snapshot, copied for it, must not put the index into its prompt
    assert "MCP tools" not in build_system_prompt(
        "base", cwd=tmp_path, config=Settings(), mcp=None, frozen=dict(frozen)
    )


def test_the_mcp_index_is_not_frozen_empty_before_the_servers_were_started(tmp_path, monkeypatch):
    from k3code.config import Settings
    from k3code.prompting import build_system_prompt

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    manager = _mcp_with_tools("go")
    manager._runners["s"].state.tools = []  # nothing connected yet
    assert manager.cold  # configured but not started: a size estimate before the first turn builds the prompt too
    frozen: dict = {}
    assert "MCP tools" not in build_system_prompt("b", cwd=tmp_path, config=Settings(), mcp=manager, frozen=frozen)
    manager._started = True
    manager._runners["s"].state.tools = _mcp_with_tools("go")._runners["s"].state.tools
    assert "mcp__s__{go}" in build_system_prompt("b", cwd=tmp_path, config=Settings(), mcp=manager, frozen=frozen)


def _shared_block(server, live, repo, n: int) -> str:
    """The system block the children of one type share, as a sub-agent spawned now gets it."""
    from k3code.subagents.runner import Handle, child_reliability
    from k3code.subagents.types import load_agent_types

    atype = load_agent_types(str(repo))["explorer"]
    h = Handle(
        id=f"sa-{n}", description="look", agent_type="explorer", tier="main", depth=1, parent_sid=live.session_id
    )
    loop = server.subagents.build_loop(live, h, atype, repo, child_reliability(server.config, h.id, server._home()))
    return loop.system_prompt


async def test_siblings_share_the_system_block_whatever_was_written_to_disk_between_them(tmp_path, monkeypatch):
    from k3code.memory import write_learned
    from m1cmd_helpers import git_repo

    repo = git_repo(tmp_path / "repo")
    server, _ = make_server(tmp_path, monkeypatch, replies=["x"])
    live = server.live_for(server.store.get(await new_session(server, repo)))
    parent_prompt = server._system_prompt_for(live)  # the parent's first prompt: its snapshot of the notes and files
    snapshot = dict(live.prompt_frozen)
    (repo / "AGENTS.md").write_text("- a convention written after the parent's prompt was built\n")
    first = _shared_block(server, live, repo, 1)
    write_learned(repo, ["a note the background review wrote between the siblings"])
    second = _shared_block(server, live, repo, 2)
    assert first == second, "siblings share one cached block"
    assert "background review" not in second and "a convention written after" not in second
    assert "background review" not in parent_prompt
    assert live.prompt_frozen == snapshot, "a child works on a copy: nothing it read is stored in the parent's snapshot"
    live.refresh_prompt_inputs()  # a compaction: the children after it see what was written
    third = _shared_block(server, live, repo, 3)
    assert "background review" in third and "a convention written after" in third
    await server.close()


class _ReusedIds:
    """A local server that numbers a reply's calls from zero every time: three replies with a ``call_0``."""

    name = "fake"
    base_url = "fake://"

    def __init__(self) -> None:
        self.requests: list[list] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        from k3code.providers.types import Message, StreamEvent, ToolCall, Usage

        self.requests.append(list(messages))
        if len(self.requests) <= 3:
            path = f"f{len(self.requests) - 1}.txt"
            calls = [ToolCall(id="call_0", name="read", arguments={"path": path})]
            reply = Message(role="assistant", content=None, tool_calls=calls)
        else:
            reply = Message(role="assistant", content="done")
        yield StreamEvent(type="done", message=reply, usage=Usage())

    async def aclose(self) -> None:
        return None


async def test_a_call_id_the_conversation_already_has_is_renamed_when_the_reply_arrives(tmp_path):
    from k3code.agent.loop import AgentLoop
    from k3code.router import Router, build_chain

    for n in range(3):
        (tmp_path / f"f{n}.txt").write_text(f"alpha{n}\nbeta\n")
    provider = _ReusedIds()
    loop = AgentLoop(
        Router(build_chain([provider], [["m"]]), max_retries=0),
        system_prompt="t",
        cwd=tmp_path,
        permission_mode="auto",
        context_window=100_000,  # elision is on (nowhere near the threshold: only what was elided before is)
    )
    # the first read's result was elided earlier (a decision of this session): its id is in the shared set
    loop.share_elision({"call_0"}, {})
    announced: list[str] = []
    async for event in loop.run("read it three times"):
        if event.type == "done" and event.message is not None:
            announced += [tc.id for tc in event.message.tool_calls]  # what the clients see announced
    ids = [tc.id for m in loop.turn_messages if m.role == "assistant" for tc in m.tool_calls]
    assert ids == ["call_0", "call_0_1", "call_0_2"] == announced
    results = [m for m in loop.turn_messages if m.role == "tool"]
    assert [m.tool_call_id for m in results] == ids, "each result carries the id of its call"
    last = provider.requests[-1]  # what the provider is sent after the third read: every call paired with its result
    assert [tc.id for m in last if m.role == "assistant" for tc in m.tool_calls] == ids
    assert [m.tool_call_id for m in last if m.role == "tool"] == ids
    # only the result whose id was elided is: the later ones, reusing the id, are new results
    assert "alpha0" not in last[3].content and "alpha1" in last[5].content and "alpha2" in last[7].content


def test_unique_call_ids_leaves_a_providers_unique_ids_alone():
    from k3code.providers.types import Message, ToolCall, unique_call_ids

    def reply(*ids: str) -> Message:
        return Message(role="assistant", tool_calls=[ToolCall(id=i, name="read") for i in ids])

    earlier = [reply("toolu_01"), Message(role="tool", tool_call_id="toolu_01"), reply("toolu_02", "toolu_03")]
    new = reply("toolu_04", "toolu_05")
    unique_call_ids(new, earlier)
    assert [tc.id for tc in new.tool_calls] == ["toolu_04", "toolu_05"]
    dup = reply("call_0", "call_0", "call_1")  # also within one reply; a renamed id is checked against the used ones
    unique_call_ids(dup, [reply("call_0", "call_0_1")])
    assert [tc.id for tc in dup.tool_calls] == ["call_0_2", "call_0_3", "call_1"]
    assert all(set(tc.id) <= set("abcdefghijklmnopqrstuvwxyz0123456789_-") for tc in dup.tool_calls)  # Anthropic's set


async def test_editing_the_project_memory_file_with_the_command_reaches_the_next_prompt(tmp_path, monkeypatch):
    from m1cmd_helpers import cmd

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    (tmp_path / "AGENTS.md").write_text("# Agents\n- use uv\n")
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    assert "use uv" in server._system_prompt_for(live)
    await cmd(server, "/memory edit", sid)  # the user opens the file now; what they save is read at the next turn
    (tmp_path / "AGENTS.md").write_text("# Agents\n- use pip\n")
    assert "use pip" in server._system_prompt_for(live)
    await server.close()


async def test_mcp_reload_names_the_tools_that_exist_now_in_every_live_prompt(tmp_path, monkeypatch):
    from m1cmd_helpers import cmd

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sid = await new_session(server, tmp_path)
    other = server.live_for(server.store.create(title="other", model=server.config.default_model, cwd=str(tmp_path)))
    for live in (server.live[sid], other):
        live.prompt_frozen["mcp"] = "## MCP tools (deferred)\n\nmcp__gone__{tool}"  # what the session first saw
    await cmd(server, "/mcp reload", sid)
    assert "mcp" not in server.live[sid].prompt_frozen and "mcp" not in other.prompt_frozen
    await server.close()
