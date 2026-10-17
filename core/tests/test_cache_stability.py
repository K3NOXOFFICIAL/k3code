"""The prompt prefix a provider caches (system prompt, then the tools array) is byte-stable.

#69/#70 worried that set/dict iteration could reorder it. Sets are only used for membership; these tests pin that,
across interpreter runs with different string hash seeds (which is what reorders a set). #168: a deferred MCP tool that
``mcp_tool_search`` activates is appended to the tools array (never inserted) and stays for later loops."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PROBE = """
import json, sys
from pathlib import Path
from k3code.config import Settings
from k3code.prompting import build_system_prompt
from k3code.tools import build_registry

from k3code.providers.types import ToolSpec


def registry():
    reg = build_registry()
    for name in ("mcp__s__b", "mcp__s__a", "mcp__s__c"):  # deferred, like MCP tools
        reg.register(ToolSpec(name=name, description="", parameters={}), None, deferred=True)
    return reg


reg = registry()
plain = [s.name for s in reg.specs()]
deferred = sorted(reg._deferred)
reg.activate(deferred[::-1])
reverse = [s.name for s in reg.specs()]
reg2 = registry()
reg2.activate(deferred)
forward = [s.name for s in reg2.specs()]
prompt = build_system_prompt("BASE", cwd=Path(sys.argv[1]), config=Settings())
print(json.dumps({"plain": plain, "reverse": reverse, "forward": forward, "prompt": prompt}))
"""


def _run(seed: str, cwd: Path, home: Path) -> dict:
    env = {"PYTHONHASHSEED": seed, "K3CODE_HOME": str(home), "HOME": str(home), "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, "-c", _PROBE, str(cwd)], env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.splitlines()[-1])


def test_prompt_and_tools_array_do_not_depend_on_hash_seed_or_activation_order(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "K3CODE.md").write_text("# memory\n- one\n- two\n")
    runs = [_run(seed, cwd, tmp_path / "home") for seed in ("0", "1", "2", "12345")]
    first = runs[0]
    for other in runs[1:]:
        assert other["prompt"] == first["prompt"]
        assert other["plain"] == first["plain"]
        assert other["forward"] == first["forward"]
    # activation appends after the advertised tools, in activation order (#168: never inserted mid-array)
    deferred = ["mcp__s__a", "mcp__s__b", "mcp__s__c"]
    assert not set(deferred) & set(first["plain"])
    assert first["forward"] == [*first["plain"], *deferred]
    assert first["reverse"] == [*first["plain"], *deferred[::-1]]


# ── #168: activating a deferred MCP tool appends to the tools array, and later loops keep it ──


class _FakeMcp:
    """What the registry, the prompt and the web tools read of an ``McpManager``: two connected tools, no process."""

    def __init__(self) -> None:
        from k3code.mcpclient import McpToolInfo

        self._tools = [
            McpToolInfo("fake", "add", "Add two integers.", {"type": "object", "properties": {"a": {}, "b": {}}}),
            McpToolInfo("fake", "echo", "Echo the text back.", {"type": "object", "properties": {"text": {}}}),
        ]

    def tools(self):
        return list(self._tools)

    def search(self, query: str, limit: int = 8):
        return [t for t in self._tools if query.strip() in (t.qualified, t.name)]

    async def call(self, qualified: str, args: dict):
        return {"content": qualified}

    async def ensure_started(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _names(specs) -> list[str]:
    return [s.name for s in specs]


async def test_an_activated_mcp_tool_is_appended_so_the_earlier_tools_array_stays_a_prefix():
    """The registry listed an activated tool at its registration position: after ``skill``, before ``mcp_tool_search``
    and every tool registered later. The array before the activation was then no prefix of the array after it, and
    the provider's prompt cache (its last breakpoint sits on the last tool) missed from that point on."""
    from k3code.extratools import register_mcp_tools, register_skill_tool
    from k3code.tools import build_registry

    reg = build_registry()
    register_skill_tool(reg, Path("."), [])
    register_mcp_tools(reg, _FakeMcp())
    reg.register(
        type(reg.get("read")[0])(name="task", description="a tool registered after the MCP ones", parameters={}),
        lambda *a, **k: None,
    )
    before = _names(reg.specs())
    assert "mcp__fake__add" not in before and before[-1] == "task"
    _, search = reg.get("mcp_tool_search")
    await search({"query": "mcp__fake__echo"})
    first = _names(reg.specs())
    await search({"query": "mcp__fake__add"})
    second = _names(reg.specs())
    assert first == [*before, "mcp__fake__echo"]
    assert second == [*first, "mcp__fake__add"]  # activation order, not registration order
    await search({"query": "mcp__fake__echo"})  # activating it again changes nothing
    assert _names(reg.specs()) == second


async def test_activated_mcp_tools_survive_the_next_loop_of_the_session(tmp_path, monkeypatch):
    """The gateway builds a fresh registry per loop (each turn, and each tier after an escalation): the activation was
    lost, the model had to search again, and the array changed under the cache. The session keeps it now, persisted
    with its meta, so a later loop (also after a daemon restart) starts with the same tools in the same order."""
    from k3code.routing.tiers import TaskKind
    from m1cmd_helpers import make_server, new_session

    server, _provider = make_server(tmp_path, monkeypatch)
    server.mcp = _FakeMcp()  # type: ignore[assignment]
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    try:
        first = server._build_loop(live, None, server.router, TaskKind.INTERACTIVE_TURN, None)
        before = _names(first.tool_specs())
        _, search = first.tools.get("mcp_tool_search")
        await search({"query": "mcp__fake__add"})
        after = _names(first.tool_specs())
        assert after == [*before, "mcp__fake__add"]

        escalated = server._build_loop(live, None, server.router, TaskKind.INTERACTIVE_TURN, None, escalates=True)
        assert _names(escalated.tool_specs()) == after  # the next tier's loop: same tools, same order

        server.store.save(live.stored)  # what the end of a turn does
        server.live.pop(sid)
        resumed = server.live_for(server.store.get(sid))
        later = server._build_loop(resumed, None, server.router, TaskKind.INTERACTIVE_TURN, None)
        assert _names(later.tool_specs()) == after
    finally:
        await server.close()
