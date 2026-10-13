"""What a session sends as its prefix must not change under it: the provider caches the system prompt and the tool list
and re-writes the whole conversation after any change to either."""

from __future__ import annotations

from k3code.agent.loop import AgentLoop
from k3code.memory import write_learned
from k3code.permissions import PermissionMode
from k3code.router import Router, build_chain
from m1cmd_helpers import make_server, new_session
from test_auto_compaction import Recording, long_history, seed


async def test_a_note_the_background_review_writes_does_not_change_the_sessions_prompt(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    live = server.live[await new_session(server, tmp_path)]
    write_learned(tmp_path, ["the tests live in tests/unit"])
    first = server._system_prompt_for(live)
    assert "the tests live in tests/unit" in first
    write_learned(tmp_path, ["the tests live in tests/unit", "run ruff before committing"])  # the review wrote one more
    assert server._system_prompt_for(live) == first, "the prompt is byte-identical, so the cache keeps the conversation"
    live.learned_notes = None  # what compaction and /clear do
    assert "run ruff before committing" in server._system_prompt_for(live)
    await server.close()


async def test_compaction_refreshes_the_learned_notes(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording(), context={"keep_messages": 6})
    live.stored.messages = long_history()
    live.perms.cwd = tmp_path
    write_learned(tmp_path, ["first note"])
    server._system_prompt_for(live)
    write_learned(tmp_path, ["first note", "second note"])
    assert "second note" not in server._system_prompt_for(live)
    assert await server.compact_session(live) > 0
    assert "second note" in server._system_prompt_for(live)
    await server.close()


async def test_a_new_session_reads_the_notes_as_they_are_now(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    write_learned(tmp_path, ["a note from yesterday"])
    live = server.live[await new_session(server, tmp_path)]
    assert "a note from yesterday" in server._system_prompt_for(live)
    await server.close()


def loop_with(tmp_path, *, plan_callback) -> AgentLoop:
    router = Router(build_chain([Recording()], [["m"]]), max_retries=0)
    return AgentLoop(router, system_prompt="t", cwd=tmp_path, permission_mode="auto", plan_callback=plan_callback)


async def _approve(plan: str) -> str:
    return "auto"


def test_a_session_loop_offers_exit_plan_in_every_mode_so_its_tool_list_never_changes(tmp_path):
    loop = loop_with(tmp_path, plan_callback=_approve)
    before = [s.name for s in loop.tool_specs()]
    loop.permissions.mode = PermissionMode.PLAN
    during = [s.name for s in loop.tool_specs()]
    loop.permissions.mode = PermissionMode.AUTO
    after = [s.name for s in loop.tool_specs()]
    assert before == during == after and "exit_plan" in before


def test_a_loop_without_a_plan_approver_still_hides_exit_plan_outside_plan_mode(tmp_path):
    loop = loop_with(tmp_path, plan_callback=None)
    assert "exit_plan" not in [s.name for s in loop.tool_specs()]
    loop.permissions.mode = PermissionMode.PLAN
    assert "exit_plan" in [s.name for s in loop.tool_specs()]


async def test_exit_plan_outside_plan_mode_says_so(tmp_path):
    loop = loop_with(tmp_path, plan_callback=_approve)
    result = await loop._exit_plan({"plan": "do it"})
    assert result == {"error": "exit_plan is only available in plan mode"}


async def test_moving_the_workspace_shows_the_new_projects_notes_not_the_old_ones(tmp_path, monkeypatch):
    from m1cmd_helpers import rpc

    a, b = tmp_path / "projA", tmp_path / "projB"
    a.mkdir()
    b.mkdir()
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    live = server.live[await new_session(server, a)]
    write_learned(a, ["A-NOTE: the tests live in tests/unit"])
    write_learned(b, ["B-NOTE: run make check"])
    assert "A-NOTE" in server._system_prompt_for(live)
    await rpc(server, "session.workspace.move", {"path": str(b)})
    prompt = server._system_prompt_for(live)
    assert "B-NOTE" in prompt and "A-NOTE" not in prompt
    await server.close()


async def test_what_other_sessions_and_background_writers_change_does_not_reach_a_live_prompt(tmp_path, monkeypatch):
    from k3code import memory as memory_mod

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    live = server.live[await new_session(server, tmp_path)]
    user_file = memory_mod.user_memory_path()
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("- I like short answers\n")
    first = server._system_prompt_for(live)
    assert "I like short answers" in first
    # the daily distill (run by another session) rewrites USER.md; a stack re-scan and an accepted gotcha land too
    user_file.write_text("- I like short answers\n\n## Learned preferences (auto)\n- rolls back changes often\n")
    assert server._system_prompt_for(live) == first, "byte-identical: the provider keeps the conversation cached"
    live.refresh_prompt_inputs()  # a compaction, /clear or a move to another project
    assert "rolls back changes often" in server._system_prompt_for(live)
    await server.close()


async def test_a_user_memory_line_added_with_the_command_reaches_the_live_prompt(tmp_path, monkeypatch):
    from k3code import memory as memory_mod
    from m1cmd_helpers import cmd

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    user_file = memory_mod.user_memory_path()
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("- I like short answers\n")
    assert "I like short answers" in server._system_prompt_for(live)  # the session has seen the file
    await cmd(server, "/memory add --user always answer in French", sid)
    await cmd(server, "/memory add the tests live in tests/unit", sid)
    prompt = server._system_prompt_for(live)
    assert "always answer in French" in prompt, "the user-scope line is the user's own: it applies at once"
    assert "the tests live in tests/unit" in prompt and "I like short answers" in prompt
    await server.close()


async def test_clear_shows_the_notes_learned_since_the_prompt_was_built(tmp_path, monkeypatch):
    from m1cmd_helpers import rpc

    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    write_learned(tmp_path, ["first note"])
    first = server._system_prompt_for(live)
    write_learned(tmp_path, ["first note", "second note"])
    assert server._system_prompt_for(live) == first
    await rpc(server, "slash.exec", {"command": "clear", "session_id": sid})
    assert "second note" in server._system_prompt_for(live)
    await server.close()


async def test_confirming_project_commands_with_proposals_accept_reaches_the_live_prompt(tmp_path, monkeypatch):
    from k3code.learning import projectstate
    from m1cmd_helpers import cmd

    (tmp_path / ".git").mkdir()
    server, _ = make_server(tmp_path, monkeypatch, ["ok"])
    stack = {"id": "python", "dir": ".", "manager": "uv", "commands": {"test": "pytest"}}
    projectstate.update(tmp_path, lambda state: state.update(stacks=[stack]))
    sid = await new_session(server, tmp_path)
    live = server.live[sid]
    first = server._system_prompt_for(live)
    assert "`pytest`" in first and "(confirmed)" not in first, "the facts block is taken, scanned commands only"
    payload = {
        "op": "recipe",
        "type": "commands",
        "root": str(tmp_path),
        "stack": "python",
        "dir": ".",
        "commands": {"test": "pytest -x"},
    }
    proposal = server.autonomy.proposals.add("commands", "confirm the project commands", "", payload=payload)
    accepted = await cmd(server, f"/proposals accept {proposal.id}", sid)
    assert "project commands confirmed" in str(accepted), accepted
    prompt = server._system_prompt_for(live)
    assert "python (confirmed)" in prompt and "`pytest -x`" in prompt, "the user's confirmation applies at once"
    await server.close()


def test_the_skills_index_is_taken_once_per_session(tmp_path, monkeypatch):
    """Past the limit the listed set depends on what a re-scan or a pin changed, and a new SKILL.md changes the
    section: either rewrote the prompt, and with it the provider's cache of the conversation, in mid-session."""
    from k3code.config import Settings
    from k3code.prompting import build_system_prompt

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))  # no ~/.claude/skills of the machine's own
    skills = tmp_path / "k3home" / "skills"

    def add_skill(name: str, description: str) -> None:
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody\n")

    frozen: dict = {}  # a session's
    project = tmp_path / "project"
    project.mkdir()

    def build() -> str:
        return build_system_prompt("BASE", cwd=project, config=Settings(), frozen=frozen)

    assert "## Skills" not in build()
    add_skill("deploy", "ship it")
    first = build()
    assert "- deploy: ship it" in first, "a session that started without skills still lists the first one"
    add_skill("added-mid-session", "x")
    assert build() == first, "byte-identical: the provider keeps the conversation cached"
    frozen.clear()  # a compaction, /clear or a move to another project (LiveSession.refresh_prompt_inputs)
    assert "- added-mid-session: x" in build()
