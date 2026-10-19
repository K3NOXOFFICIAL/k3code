"""Every AgentLoop the product builds gets the context window of the model its router calls, so in-turn elision of old
tool results works there too (it did only in the interactive gateway; `-p`, the REPL, sub-agents and the planner grew
until the provider rejected the request)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from k3code import cli as cli_mod
from k3code import context_budget
from k3code.agent.loop import AgentLoop
from k3code.config import ProviderEntry, Settings
from k3code.permissions import PermissionMode
from k3code.providers.fake import FakeProvider
from k3code.providers.types import Message, ToolCall
from test_autonomy_gateway import make, start

MAIN, CHEAP = "m-main", "m-cheap"


def _config(**extra) -> Settings:
    prov = ProviderEntry(
        name="t",
        kind="openai",
        base_url="http://t",
        api_key_env="NOPE",
        models={"default": MAIN, "cheap": CHEAP},
    )
    models = {MAIN: {"context_window": 64_000}, CHEAP: {"context_window": 32_000}}
    return Settings(providers=[prov], default_model="default", models=models, **extra)


def _spy_loops(monkeypatch: pytest.MonkeyPatch) -> list[AgentLoop]:
    """Every AgentLoop built, with the window its constructor got kept as ``built_window``."""
    built: list[AgentLoop] = []
    real_init = AgentLoop.__init__

    def init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        real_init(self, *args, **kwargs)
        self.built_window = self.context_window
        built.append(self)

    monkeypatch.setattr(AgentLoop, "__init__", init)
    return built


def _fake_providers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    import k3code.providers as providers_mod

    monkeypatch.setattr(
        providers_mod, "make_providers", lambda entries: [FakeProvider(steps=[{"type": "text", "text": "ok"}])]
    )


def _child_loop(server):
    from k3code.subagents.runner import Handle, SubagentManager
    from k3code.subagents.types import AgentType

    session = server.session
    handle = Handle(id="c1", description="d", agent_type="general", tier="main", depth=1, parent_sid=session.session_id)
    return SubagentManager(server).build_loop(
        session, handle, AgentType(name="general", tools=["bash"]), session.perms.cwd, None
    )


def test_headless_loop_gets_the_window_of_its_model(tmp_path, monkeypatch):
    _fake_providers(monkeypatch, tmp_path)
    built = _spy_loops(monkeypatch)
    config = _config(context={"tool_output_chars": 4321})
    asyncio.run(
        cli_mod._run_headless("hi", model="cheap", permission_mode=PermissionMode.YOLO, config=config, json_output=True)
    )
    (loop,) = built
    assert loop.context_window == context_budget.context_window(config, CHEAP) == 32_000
    assert loop.tool_output_chars == 4321


def test_repl_loop_gets_the_window_and_follows_a_model_switch(tmp_path, monkeypatch):
    _fake_providers(monkeypatch, tmp_path)
    built = _spy_loops(monkeypatch)
    config = _config()
    lines = iter(["/model cheap", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(lines))
    asyncio.run(cli_mod._run_repl(model=None, permission_mode=PermissionMode.YOLO, config=config))
    (loop,) = built
    assert loop.built_window == context_budget.context_window(config, MAIN) == 64_000
    assert loop.context_window == context_budget.context_window(config, CHEAP) == 32_000  # refreshed by /model


async def test_subagent_loop_gets_the_window_of_its_models(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], models={"m-main": {"context_window": 48_000}})
    await start(server, tmp_path)
    loop = _child_loop(server)
    assert loop.context_window == context_budget.context_window(server.config, "m-main") == 48_000
    assert loop.tool_output_chars is None  # unset in config: the default clip


async def test_planner_loop_gets_the_window_of_its_models(tmp_path, monkeypatch):
    from k3code.autonomy.plan_first import PlanFirst
    from k3code.autonomy.scope import ScopeVerdict

    built = _spy_loops(monkeypatch)
    server = make(
        tmp_path,
        monkeypatch,
        [{"type": "text", "text": "the plan"}],
        models={"m-strong": {"context_window": 40_000}},
        context={"tool_output_chars": 777},
    )
    await start(server, tmp_path)
    verdict = ScopeVerdict(scope="small", risk="low", reason="r")
    await PlanFirst(server)._planning_turn(server.session, "do it", verdict)
    planner = built[-1]
    assert planner.task_kind.value == "plan"
    assert planner.context_window == context_budget.context_window(server.config, "m-strong") == 40_000
    assert planner.tool_output_chars == 777


async def test_subagent_with_a_small_window_elides_an_old_large_tool_result(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], models={"m-main": {"context_window": 4_000}})
    await start(server, tmp_path)
    loop = _child_loop(server)
    big = "x" * 6000  # over ELIDE_MIN_CHARS; with the rest well over half of a 4000-token window
    history = [Message(role="user", content="start")]
    for i in range(context_budget.ELIDE_KEEP_CALLS + 2):
        call = ToolCall(id=f"c{i}", name="bash", arguments={"command": "echo"})
        history += [
            Message(role="assistant", content="", tool_calls=[call]),
            Message(role="tool", content=big, tool_call_id=f"c{i}", name="bash"),
        ]
    wire = await loop._request_messages(history, [])
    assert context_budget.elide_marker("bash", len(big)) in [m.content for m in wire]
    assert wire[2].content != big  # the oldest result is gone from the request...
    assert history[2].content == big  # ...but not from the transcript
