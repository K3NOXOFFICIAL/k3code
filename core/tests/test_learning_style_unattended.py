"""The ``learning`` output style asks the model to leave a TODO(human); nobody can fill one in an unattended run."""

from __future__ import annotations

from pathlib import Path

from k3code.config import Settings
from k3code.prompting import build_system_prompt
from k3code.routing.tiers import TaskKind
from k3code.subagents.runner import Handle, SubagentManager
from k3code.subagents.types import AgentType
from test_autonomy_gateway import make, start


def _cfg() -> Settings:
    return Settings(output_style="learning")


def test_an_attended_prompt_keeps_the_learning_todo(tmp_path):
    prompt = build_system_prompt("BASE", cwd=tmp_path, config=_cfg())
    assert "TODO(human)" in prompt


def test_an_unattended_prompt_drops_the_learning_todo_but_keeps_the_teaching(tmp_path):
    prompt = build_system_prompt("BASE", cwd=tmp_path, config=_cfg(), attended=False)
    assert "TODO(human)" not in prompt
    assert "Output style: learning" in prompt and "explain key decisions" in prompt


def test_a_custom_style_file_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    styles = tmp_path / "home" / "output-styles"
    styles.mkdir(parents=True)
    (styles / "learning.md").write_text("My own style mentions TODO(human).")
    prompt = build_system_prompt("BASE", cwd=tmp_path, config=_cfg(), attended=False)
    assert "My own style mentions TODO(human)." in prompt


async def test_a_session_turn_drops_the_todo_only_when_unattended(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], mode="default", output_style="learning", autonomy={"plan_first": False})
    await start(server, tmp_path)
    session = server.session
    assert "TODO(human)" in server._build_loop(session, None, None, TaskKind.INTERACTIVE_TURN, None).system_prompt
    session.goal_continuation = True  # a /goal turn the user is not watching
    assert "TODO(human)" not in server._build_loop(session, None, None, TaskKind.INTERACTIVE_TURN, None).system_prompt
    session.goal_continuation = False
    session.background = True  # a cron or loop tick
    assert "TODO(human)" not in server._build_loop(session, None, None, TaskKind.INTERACTIVE_TURN, None).system_prompt


async def test_a_sub_agent_prompt_has_no_todo(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], mode="default", output_style="learning")
    await start(server, tmp_path)
    session = server.session
    handle = Handle(id="c1", description="d", agent_type="general", tier="main", depth=1, parent_sid=session.session_id)
    atype = AgentType(name="general", tools=["read"])
    loop = SubagentManager(server).build_loop(session, handle, atype, Path(tmp_path), None)
    assert "Output style: learning" in loop.system_prompt and "TODO(human)" not in loop.system_prompt
