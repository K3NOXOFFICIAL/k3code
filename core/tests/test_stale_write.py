"""#100: write and edit refuse an existing file the session has not read, or one that changed on disk since the session
last read, wrote or edited it (the user's editor, a formatter, another agent). Before, write replaced the whole file and
the other change was gone without a word."""

from __future__ import annotations

from k3code.agent.loop import ApprovalResult
from k3code.permissions.engine import PermissionMode
from k3code.providers.types import ToolCall
from k3code.reliability import Reliability
from k3code.router import Router, build_chain
from k3code.routing.tiers import TaskKind
from test_auto_compaction import Recording, seed
from test_token_context_budget import make_loop
from test_wire_clip import Recorder, text_reply, tool_reply


class Editor(Recorder):
    """A Recorder that also plays the user: before reply n it runs ``saves[n]`` (an editor saving the file)."""

    def __init__(self, replies, saves) -> None:
        super().__init__(replies)
        self.saves = saves

    async def stream(self, messages, tools, model, **kw):
        if (save := self.saves.get(len(self.requests))) is not None:
            save()
        async for event in super().stream(messages, tools, model, **kw):
            yield event


async def run_script(tmp_path, script, saves=None) -> dict[str, str]:
    provider = Editor([*(tool_reply(c) for c in script), text_reply("done")], saves or {})
    loop = make_loop(tmp_path, provider, window=None)
    async for _ in loop.run("change the file"):
        pass
    return {m.tool_call_id: m.content for m in loop.turn_messages if m.role == "tool"}


async def test_a_write_after_the_user_saved_the_file_is_refused_and_the_users_edit_kept(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("x = 1\n")
    script = [
        ToolCall(id="r", name="read", arguments={"path": str(f)}),
        ToolCall(id="w", name="write", arguments={"path": str(f), "content": "x = 2\n"}),
    ]
    # reply 1 (the write) is played after the read ran: the user saves the file in between
    results = await run_script(tmp_path, script, {1: lambda: f.write_text("x = 1\ny = 'mine'\n")})
    assert results["w"].startswith("Error: ")
    assert "changed since you read it" in results["w"] and "read it again" in results["w"]
    assert f.read_text() == "x = 1\ny = 'mine'\n"


async def test_a_write_over_an_existing_file_that_was_never_read_is_refused(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("keep me\n")
    results = await run_script(tmp_path, [ToolCall(id="w", name="write", arguments={"path": str(f), "content": "x"})])
    assert results["w"].startswith("Error: ")
    assert "read it before" in results["w"]
    assert f.read_text() == "keep me\n"


async def test_an_edit_needs_a_read_too_and_a_current_one(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("a = 1\nb = 2\n")
    edit = {"path": str(f), "old_string": "a = 1", "new_string": "a = 10"}
    script = [
        ToolCall(id="e1", name="edit", arguments=edit),  # never read: refused
        ToolCall(id="r", name="read", arguments={"path": str(f)}),
        ToolCall(id="e2", name="edit", arguments=edit),  # the user changed it after the read: refused
    ]
    results = await run_script(tmp_path, script, {2: lambda: f.write_text("a = 1\nb = 3\n")})
    assert "read it before" in results["e1"]
    assert "changed since you read it" in results["e2"]
    assert f.read_text() == "a = 1\nb = 3\n"


async def test_what_the_session_wrote_edited_or_read_again_it_may_change(tmp_path):
    new = tmp_path / "new.py"
    f = tmp_path / "f.py"
    f.write_text("v = 1\n")
    script = [
        ToolCall(id="w1", name="write", arguments={"path": str(new), "content": "n = 1\n"}),  # a new file
        ToolCall(id="e1", name="edit", arguments={"path": str(new), "old_string": "n = 1", "new_string": "n = 2"}),
        ToolCall(id="w2", name="write", arguments={"path": str(new), "content": "n = 3\n"}),  # after its own edit
        ToolCall(id="r1", name="read", arguments={"path": str(f)}),
        ToolCall(id="w3", name="write", arguments={"path": str(f), "content": "v = 2\n"}),  # refused: changed
        ToolCall(id="r2", name="read", arguments={"path": str(f)}),  # read it again ...
        ToolCall(id="w4", name="write", arguments={"path": str(f), "content": "v = 2\n"}),  # ... then it may
    ]
    results = await run_script(tmp_path, script, {4: lambda: f.write_text("v = 1  # theirs\n")})
    assert [results[k] for k in ("w1", "e1", "w2")] == [f"Wrote {new}", "Edited: 1 replacement", f"Wrote {new}"]
    assert new.read_text() == "n = 3\n"
    assert "changed since you read it" in results["w3"]
    assert "theirs" in results["r2"]
    assert results["w4"] == f"Wrote {f}"
    assert f.read_text() == "v = 2\n"


async def test_the_check_runs_after_the_approval_prompt_never_before_it(tmp_path):
    """Before the prompt a refusal would tell the model whether a file exists without anyone being asked (a read outside
    the project needs a human); after it, it catches a save made while the prompt was open."""
    f = tmp_path / "f.py"
    f.write_text("x = 1\n")
    asked: list[str] = []

    async def approve(tool, args, decision):
        asked.append(args.get("content", ""))
        if args.get("content") == "deny me":
            return ApprovalResult("deny")
        f.write_text("x = 1\ny = 'saved while the prompt was open'\n")
        return ApprovalResult("once")

    script = [
        ToolCall(id="w1", name="write", arguments={"path": str(f), "content": "deny me"}),  # not read: asked first
        ToolCall(id="r", name="read", arguments={"path": str(f)}),
        ToolCall(id="w2", name="write", arguments={"path": str(f), "content": "x = 2\n"}),
    ]
    provider = Recorder([*(tool_reply(c) for c in script), text_reply("done")])
    loop = make_loop(tmp_path, provider, window=None)
    loop.permissions.mode, loop.headless, loop.approval_callback = PermissionMode.DEFAULT, False, approve
    async for _ in loop.run("change the file"):
        pass
    results = {m.tool_call_id: m.content for m in loop.turn_messages if m.role == "tool"}
    assert asked == ["deny me", "x = 2\n"]
    assert "User denied" in results["w1"] and "read it" not in results["w1"]
    assert "changed since you read it" in results["w2"]
    assert f.read_text() == "x = 1\ny = 'saved while the prompt was open'\n"


async def test_a_read_in_one_turn_lets_the_next_turn_write_the_unchanged_file(tmp_path, monkeypatch):
    """The gateway builds a loop per turn (and per tier): what the session has read is kept with the session."""
    server, live = await seed(tmp_path, monkeypatch, Recording())
    live.perms.mode = PermissionMode.YOLO
    rel = Reliability.from_settings(None, session=live.session_id, home=tmp_path / "home")
    f = tmp_path / "f.py"
    f.write_text("x = 1\n")

    async def turn(call: ToolCall) -> str:
        provider = Recorder([tool_reply(call), text_reply("done")])
        router = Router(build_chain([provider], [["m"]]), max_retries=0)
        loop = server._build_loop(live, rel, router, TaskKind.INTERACTIVE_TURN, None)
        async for _ in loop.run("go"):
            pass
        return next(m.content for m in loop.turn_messages if m.role == "tool")

    await turn(ToolCall(id="r", name="read", arguments={"path": str(f)}))
    assert await turn(ToolCall(id="w", name="write", arguments={"path": str(f), "content": "x = 2\n"})) == f"Wrote {f}"
    f.write_text("x = 2\ny = 'mine'\n")  # between turns the user saves the file
    result = await turn(ToolCall(id="w2", name="write", arguments={"path": str(f), "content": "x = 3\n"}))
    assert "changed since you read it" in result
    assert f.read_text() == "x = 2\ny = 'mine'\n"
    await server.close()
