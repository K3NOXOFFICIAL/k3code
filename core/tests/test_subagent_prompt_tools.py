"""A sub-agent's system prompt names only the tools the child has, and what its types share comes first (#166)."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

from k3code import prompting
from k3code.subagents import runner
from k3code.tools import build_registry
from test_autonomy_gateway import make, run_turn, start
from test_subagents import final, task_call

TYPES = ["explorer", "planner", "reviewer", "worker"]
#: every tool name that exists somewhere in k3code: the ones a child may be told about only if it has them
EXTRA_TOOLS = {"skill", "task", "task_result", "advisor", "ask_user_question", "mcp_tool_search", "mcp__demo__lookup"}


def named_tools(text: str) -> set[str]:
    return {n for n in re.findall(r"`([A-Za-z0-9_]+)`", text) if n in {*build_registry().names(), *EXTRA_TOOLS}}


async def spawn(tmp_path, monkeypatch, agent_type: str, description: str = "child job", project=None):
    """Run one child of ``agent_type`` in a session that has an advisor, an MCP server and a skill; return its loop."""
    skills = tmp_path / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: demo\ndescription: a demo skill\n---\nbody\n", encoding="utf-8")
    steps = [
        task_call(f"CHILD-{agent_type}", agent_type=agent_type, description=description),
        final("ok"),
        {"type": "text", "match": f"[agent:{agent_type}]", "text": "done"},
    ]
    server = make(
        tmp_path,
        monkeypatch,
        steps,
        autonomy={"plan_first": False, "proposals": False, "advisor": "strong"},
        skills={"roots": [str(tmp_path / "skills")], "import_claude": False},
    )
    tool = SimpleNamespace(
        server="demo", name="lookup", qualified="mcp__demo__lookup", description="look it up", schema=None
    )

    async def nothing():
        return None

    # one connected MCP tool: the parent's prompt lists it
    server.mcp = SimpleNamespace(tools=lambda: [tool], ensure_started=nothing, close=nothing)
    built = []
    real_build = runner.SubagentManager.build_loop

    def spy(self, *args, **kwargs):  # a finished child drops its loop: keep a reference
        built.append(real_build(self, *args, **kwargs))
        return built[-1]

    monkeypatch.setattr(runner.SubagentManager, "build_loop", spy)
    project = project or tmp_path / "proj"
    project.mkdir(exist_ok=True)
    await start(server, project)
    await run_turn(server, "PARENT: delegate it")
    (loop,) = built
    return server, loop


@pytest.mark.parametrize("agent_type", TYPES)
async def test_a_child_prompt_names_no_tool_the_child_lacks(tmp_path, monkeypatch, agent_type):
    server, loop = await spawn(tmp_path, monkeypatch, agent_type)
    # the session this child belongs to would tell its own model about all three; the child must not hear of them
    parent = prompting.build_system_prompt(
        prompting.base_prompt(), cwd=tmp_path / "proj", config=server.config, mcp=server.mcp
    )
    assert "mcp_tool_search" in parent and "`advisor`" in parent and "`skill`" in parent
    have = set(loop.tools.names())
    extra = named_tools(loop.system_prompt) - have
    assert not extra, f"the {agent_type} child's prompt names {sorted(extra)} but its tools are {sorted(have)}"
    assert ("## Skills" in loop.system_prompt) == ("skill" in have)  # an index only where there is a tool to load it


@pytest.mark.parametrize("agent_type", TYPES)
async def test_a_child_prompt_keeps_the_guidance_for_the_tools_it_has(tmp_path, monkeypatch, agent_type):
    _server, loop = await spawn(tmp_path, monkeypatch, agent_type)
    for name in {"read", "grep", "glob"} & set(loop.tools.names()):
        assert f"`{name}`" in loop.system_prompt, (agent_type, name)
    assert ("`edit`" in loop.system_prompt) == ("edit" in loop.tools.names())
    assert ("`todo`" in loop.system_prompt) == ("todo" in loop.tools.names())


async def test_what_children_share_comes_before_what_differs_per_type_and_task(tmp_path, monkeypatch):
    project = tmp_path / "proj"  # one project: its scope section is the same in every child
    prompts = {}
    for i, agent_type in enumerate(TYPES):
        (tmp_path / str(i)).mkdir()
        loop = (await spawn(tmp_path / str(i), monkeypatch, agent_type, f"task number {i}", project))[1]
        prompts[agent_type] = loop.system_prompt
    common = os.path.commonprefix(list(prompts.values()))
    assert prompting.scope_prompt(project) in common  # the base and the session's own sections are shared by all
    assert "## Principles" in common
    assert "[agent:" not in common and "task number" not in common
    for agent_type, text in prompts.items():
        assert text.count("[agent:") == 1, agent_type
        assert text.index("[agent:") > len(common), agent_type  # the per-task line comes after what is shared
    # the same type with another task differs only from the task line on
    (tmp_path / "again").mkdir()
    other = (await spawn(tmp_path / "again", monkeypatch, "explorer", "a different task", project))[1]
    cut = prompts["explorer"].index("[agent:explorer]")
    assert other.system_prompt[:cut] == prompts["explorer"][:cut]
    assert other.system_prompt != prompts["explorer"]


def test_the_base_prompt_names_tools_only_in_its_tool_use_section():
    # what lets every child share the base up to that section: nothing before it depends on the child's tools
    head, tool_use = prompting.split_tool_use(prompting.base_prompt())
    assert tool_use.startswith(prompting.TOOL_USE_HEADING) and tool_use.count("\n## ") == 0
    assert not named_tools(head)
    assert prompting.tool_use_for(tool_use, set()).count("`") == 0  # lines that name no tool stay for a child with none


def test_the_dumped_sub_agent_modes_name_only_their_types_tools(tmp_path, monkeypatch):
    from k3code.config import Settings
    from k3code.subagents.types import load_agent_types

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    for name, atype in load_agent_types(tmp_path).items():
        text, _sections = prompting.assemble_mode(f"subagent:{name}", cwd=tmp_path, config=Settings())
        if atype.tools:
            assert not named_tools(text) - set(atype.tools), name
        assert text.index("[agent:") > text.index("## Session scope"), name
