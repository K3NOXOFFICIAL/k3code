"""The read-before-write guard (#100) at its seams: the permission layer (.env cannot be read), PostToolUse hooks, the
approval prompt (what it costs, what it shows, what it offers), sub-agents in a worktree, the learning hub and the
failed-calls list."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from k3code import userhooks
from k3code.agent.loop import ApprovalResult
from k3code.learning import gotchas
from k3code.permissions.engine import PermissionMode, inside_roots
from k3code.providers.types import ToolCall
from k3code.toolerrors import describe_call, failure_of
from k3code.tools import HINT_MAX_CHARS, FileStamps, plan_edit
from test_auto_compaction import Recording, seed
from test_learning_gateway import make_server
from test_stale_write import Editor
from test_token_context_budget import make_loop
from test_wire_clip import text_reply, tool_reply


def call(id: str, name: str, **arguments) -> ToolCall:
    return ToolCall(id=id, name=name, arguments=arguments)


def loop_for(tmp_path, script, approve=None, saves=None, mode=PermissionMode.DEFAULT, cwd=None):
    """A loop that plays ``script`` (tool calls, one per reply); ``approve`` answers the prompts, and ``saves[n]`` is
    the user saving a file just before reply n."""
    provider = Editor([*(tool_reply(c) for c in script), text_reply("done")], saves or {})
    loop = make_loop(cwd or tmp_path, provider, window=None)
    loop.permissions.mode = mode
    if approve is not None:
        loop.headless, loop.approval_callback = False, approve
    return loop


async def run(loop) -> dict[str, str]:
    async for _ in loop.run("change the file"):
        pass
    return {m.tool_call_id: m.content for m in loop.turn_messages if m.role == "tool"}


class Prompts:
    """An approval callback that says yes and remembers what it was asked."""

    def __init__(self, on_ask=None) -> None:
        self.asked: list[tuple[str, bool]] = []
        self.on_ask = on_ask

    async def __call__(self, tool, args, decision):
        self.asked.append((tool, bool(decision.always_ask)))
        if self.on_ask is not None:
            self.on_ask()
        return ApprovalResult("once")


# ── FIX 1: a file the model may not read can still be changed ──────────────────────────────────────────────────────


async def test_an_existing_dotenv_file_is_edited_and_written_on_the_diff_alone(tmp_path):
    env = tmp_path / ".env"
    env.write_text("API_URL=http://localhost\nDEBUG=0\n")
    prompts = Prompts()
    script = [
        call("e", "edit", path=str(env), old_string="DEBUG=0", new_string="DEBUG=1"),
        call("w", "write", path=str(env), content="API_URL=http://localhost\nDEBUG=2\n"),
    ]
    results = await run(loop_for(tmp_path, script, prompts, mode=PermissionMode.YOLO))
    assert results["e"] == "Edited: 1 replacement"
    assert results["w"] == f"Wrote {env}"
    assert prompts.asked == [("edit", True), ("write", True)]  # a person confirmed each change, from its diff
    assert env.read_text() == "API_URL=http://localhost\nDEBUG=2\n"


def test_only_a_file_the_model_cannot_read_skips_the_read_first_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stamps = FileStamps()
    for name in (".env", ".env.local", ".ssh/config", ".aws/credentials"):  # sensitive_path: read is a hardline deny
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("KEY=1\n")
        assert stamps.refusal("edit", path) is None, name
    for name in ("app.py", ".env.example"):  # an ordinary file and a committed template: the model can read these
        path = tmp_path / name
        path.write_text("KEY=1\n")
        assert "have not read" in (stamps.refusal("edit", path) or ""), name


# ── FIX 2: a formatter hook after an edit is not "someone else" ────────────────────────────────────────────────────


def formatter_hook(tmp_path, target: Path) -> userhooks.HookRunner:
    fmt = tmp_path / "fmt.sh"
    fmt.write_text(
        f"#!/bin/sh\nprintf '# formatted\\n' >> {target}\n"
    )  # what prettier/black/gofmt do: rewrite the file
    fmt.chmod(0o755)
    return userhooks.HookRunner([userhooks.Hook("PostToolUse", str(fmt), "edit|write")], cwd=tmp_path)


def two_edits(f: Path) -> list[ToolCall]:
    return [
        call("r", "read", path=str(f)),
        call("e1", "edit", path=str(f), old_string="x=1", new_string="x = 10"),
        call("e2", "edit", path=str(f), old_string="y=2", new_string="y = 20"),
    ]


async def test_a_formatter_hook_does_not_make_the_next_edit_of_the_file_stale(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x=1\ny=2\n")
    loop = loop_for(tmp_path, two_edits(f), mode=PermissionMode.YOLO)
    loop.hooks = formatter_hook(tmp_path, f)
    results = await run(loop)
    assert "Edited: 1 replacement" in results["e1"] and "Edited: 1 replacement" in results["e2"]
    assert f.read_text() == "x = 10\ny = 20\n# formatted\n# formatted\n"


async def test_an_outside_change_after_a_formatted_edit_is_still_refused(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x=1\ny=2\n")
    loop = loop_for(
        tmp_path, two_edits(f), mode=PermissionMode.YOLO, saves={2: lambda: f.write_text(f.read_text() + "z=3\n")}
    )
    loop.hooks = formatter_hook(tmp_path, f)
    results = await run(loop)
    assert "Refused: " in results["e2"] and "changed since you read it" in results["e2"]
    assert f.read_text() == "x = 10\ny=2\n# formatted\nz=3\n"


# ── FIX 3: a certain refusal costs no prompt; an empty file has nothing to lose ────────────────────────────────────


async def test_an_unread_edit_inside_the_project_is_refused_without_asking(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    edit = {"path": str(f), "old_string": "x = 1", "new_string": "x = 2"}
    prompts = Prompts()
    script = [call("e1", "edit", **edit), call("r", "read", path=str(f)), call("e2", "edit", **edit)]
    results = await run(loop_for(tmp_path, script, prompts))
    assert "Refused: you have not read" in results["e1"]
    assert results["e2"] == "Edited: 1 replacement"
    assert prompts.asked == [("edit", False)]  # one prompt, for the edit that could succeed
    assert f.read_text() == "x = 2\n"


async def test_inside_the_project_a_save_while_the_prompt_is_open_is_still_caught(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    prompts = Prompts(on_ask=lambda: f.write_text("x = 1\ny = 'saved while the prompt was open'\n"))
    script = [call("r", "read", path=str(f)), call("w", "write", path=str(f), content="x = 2\n")]
    results = await run(loop_for(tmp_path, script, prompts))
    assert len(prompts.asked) == 1  # it passed the check before the prompt: the file was as read
    assert "changed since you read it" in results["w"]
    assert f.read_text() == "x = 1\ny = 'saved while the prompt was open'\n"


async def test_a_link_out_of_the_project_is_still_checked_only_after_the_prompt(tmp_path):
    """Guard (holds with and without the early check): realpath puts the target outside, so no refusal may say before
    the prompt that it exists."""
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "secret_notes.txt"
    target.write_text("private\n")
    (project / "link.txt").symlink_to(target)
    prompts = Prompts()
    script = [call("w", "write", path=str(project / "link.txt"), content="x")]
    results = await run(loop_for(tmp_path, script, prompts, cwd=project))
    assert len(prompts.asked) == 1 and "have not read" in results["w"]
    assert target.read_text() == "private\n"


async def test_write_into_an_existing_empty_file_needs_no_read(tmp_path):
    empty = tmp_path / "__init__.py"
    empty.write_text("")
    results = await run(
        loop_for(tmp_path, [call("w", "write", path=str(empty), content="VERSION = 1\n")], mode=PermissionMode.YOLO)
    )
    assert results["w"] == f"Wrote {empty}"
    assert empty.read_text() == "VERSION = 1\n"
    full = tmp_path / "full.py"
    full.write_text("keep\n")
    assert "have not read" in (FileStamps().refusal("write", full) or "")  # a file with content still does


def test_the_project_roots_are_the_cwd_and_the_added_dirs(tmp_path):
    project, extra, outside = tmp_path / "project", tmp_path / "extra", tmp_path / "outside"
    for d in (project, extra, outside):
        d.mkdir()
    (project / "link").symlink_to(outside)
    assert inside_roots(project / "a.py", project)
    assert not inside_roots(extra / "a.py", project)
    assert inside_roots(extra / "a.py", project, [str(extra)])
    assert not inside_roots(project / "link" / "a.py", project)  # a link out of the project is outside it


# ── FIX 4: the approval shows the diff of the folder the call runs in ──────────────────────────────────────────────


async def test_the_loop_tells_a_callback_that_takes_it_where_the_call_runs(tmp_path):
    told: list[object] = []

    async def takes_cwd(tool, args, decision, *, cwd=None):
        told.append(cwd)
        return ApprovalResult("once")

    async def takes_kwargs(tool, args, decision, **kw):
        told.append(kw.get("cwd"))
        return ApprovalResult("once")

    old_style = Prompts()  # three arguments, no cwd: every existing caller
    for callback in (takes_cwd, takes_kwargs, old_style):
        script = [call("w", "write", path=str(tmp_path / "new.py"), content="x = 1\n")]
        results = await run(loop_for(tmp_path, script, callback))
        assert results["w"].startswith("Wrote ")
        (tmp_path / "new.py").unlink()
    assert told == [tmp_path, tmp_path]
    assert old_style.asked == [("write", False)]


async def test_a_sub_agents_edit_is_previewed_against_the_folder_it_runs_in(tmp_path, monkeypatch):
    """The gateway's callback is the session's, shared by its sub-agents: a child in a worktree used to be shown the
    parent's file (or "this edit will fail" for an edit that works)."""
    server, live = await seed(tmp_path, monkeypatch, Recording())
    parent = Path(live.perms.cwd)
    worktree = parent / "worktrees" / "c1"
    worktree.mkdir(parents=True)
    (parent / "a.py").write_text("x = 'PARENT CHECKOUT'\n")
    (worktree / "a.py").write_text("x = 'CHILD WORKTREE'\n")
    sent: dict = {}

    async def ask(kind, payload, session_id):
        sent.update(payload)
        return {"choice": "once"}

    server._ask_client = ask
    script = [
        call("r", "read", path="a.py"),  # relative, as the child writes it: the tool resolves it in its own cwd
        call("e", "edit", path="a.py", old_string="x = 'CHILD WORKTREE'", new_string="x = 'new'"),
    ]
    loop = loop_for(tmp_path, script, await server._approval_callback_for(live), cwd=worktree)
    results = await run(loop)
    assert results["e"] == "Edited: 1 replacement"
    assert "CHILD WORKTREE" in sent["preview"] and "will fail" not in sent["preview"], sent["preview"]
    assert (worktree / "a.py").read_text() == "x = 'new'\n"
    assert (parent / "a.py").read_text() == "x = 'PARENT CHECKOUT'\n"
    await server.close()


async def test_without_a_cwd_the_gateway_previews_against_the_sessions_own(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording())
    (Path(live.perms.cwd) / "a.py").write_text("x = 'PARENT CHECKOUT'\n")
    sent: dict = {}

    async def ask(kind, payload, session_id):
        sent.update(payload)
        return {"choice": "once"}

    server._ask_client = ask
    approve = await server._approval_callback_for(live)
    await approve("edit", {"path": "a.py", "old_string": "PARENT CHECKOUT", "new_string": "new"}, None)
    assert "PARENT CHECKOUT" in sent["preview"] and "will fail" not in sent["preview"]
    await server.close()


# ── FIX 5: the guard's refusals are policy, not a gotcha of the project ────────────────────────────────────────────


def both_refusals(tmp_path) -> list[str]:
    f = tmp_path / "src" / "app.py"
    f.parent.mkdir(exist_ok=True)
    f.write_text("a = 1\n")
    stamps = FileStamps()
    unread = stamps.refusal("edit", f)
    stamps.record(f)
    f.write_text("a = 1\nb = 2\n")
    return [unread, stamps.refusal("edit", f)]


def test_the_read_first_refusals_are_classed_as_denials(tmp_path):
    for text in both_refusals(tmp_path):
        assert text.startswith("Refused: "), text
        failure = failure_of("edit", {"path": "x"}, {"error": text})
        assert failure.error_class == "denied", text


async def test_a_read_first_refusal_followed_by_a_working_retry_teaches_nothing(tmp_path, monkeypatch):
    for identical in (True, False):
        project = tmp_path / ("same" if identical else "other")
        project.mkdir()
        server, _ = make_server(project, ["ok"], monkeypatch)
        hub = server.learning
        refusal = both_refusals(project)[0]
        reminders = []
        for sid in ("s1", "s2", "s3"):
            session = SimpleNamespace(
                session_id=sid, perms=SimpleNamespace(cwd=project), background=False, emit=lambda *a, **k: None
            )
            args = {"path": f"{project}/src/app.py", "old_string": "a = 1", "new_string": "a = 10"}
            bad = ToolCall(id="x", name="edit", arguments=args)
            result = {"error": refusal}
            reminders.append(hub.tool_outcome(session, bad, result, failure_of("edit", bad.arguments, result)))
            retry = args if identical else {**args, "old_string": "a = 1\nb"}
            hub.tool_outcome(session, ToolCall(id="y", name="edit", arguments=retry), {"ok": True}, None)
        auto = gotchas.auto_gotchas_path(hub.log.project_for(str(project)))
        assert not auto.exists() or not auto.read_text().strip()
        assert [c for c in hub.store.all() if c.kind == "project_gotcha"] == []
        assert reminders == [None, None, None]


# ── FIX 6: a change that is asked every time does not offer to be remembered ──────────────────────────────────────


async def test_an_always_ask_edit_offers_once_or_deny_only(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording())
    live.perms.mode = PermissionMode.YOLO
    cwd = Path(live.perms.cwd)
    sent: dict = {}

    async def ask(kind, payload, session_id):
        sent.clear()
        sent.update(payload)
        return {"choice": "always"}  # a client that offers it anyway, or an old one

    server._ask_client = ask
    approve = await server._approval_callback_for(live)
    for name in (".env", ".husky/pre-commit", ".gitattributes"):
        args = {"path": str(cwd / name), "old_string": "a", "new_string": "b"}
        decision = live.perms.decide("edit", args)
        assert decision.always_ask, name
        answer = await approve("edit", args, decision)
        assert sent["choices"] == ["once", "deny"], name
        assert sent["allow_session"] is False and sent["allow_permanent"] is False and sent["pattern"] == ""
        assert answer.choice == "once"  # nothing was remembered, in this session or in the project's config
        assert live.perms.session_rules == []
    assert not (cwd / ".k3code").exists()
    await server.close()


async def test_an_ordinary_edit_still_offers_session_and_always(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, Recording())
    live.perms.mode = PermissionMode.DEFAULT
    sent: dict = {}

    async def ask(kind, payload, session_id):
        sent.update(payload)
        return {"choice": "session"}

    server._ask_client = ask
    approve = await server._approval_callback_for(live)
    args = {"path": str(Path(live.perms.cwd) / "a.py"), "old_string": "a", "new_string": "b"}
    decision = live.perms.decide("edit", args)
    assert decision.action == "ask" and not decision.always_ask
    answer = await approve("edit", args, decision)
    assert sent["choices"] == ["once", "session", "always", "deny"]
    assert sent["allow_session"] is True and sent["allow_permanent"] is True and sent["pattern"]
    assert answer.choice == "session" and live.perms.session_rules
    await server.close()


async def test_a_sub_agents_file_in_its_worktree_is_asked_once_not_offered_as_a_rule(tmp_path, monkeypatch):
    """A fan-out child's worktree is removed when it ends: "always" would write `edit(<repo>/.k3code/worktrees/<id>/f)`
    into the project's config.yaml for a path that can never come up again."""
    server, live = await seed(tmp_path, monkeypatch, Recording())
    live.perms.mode = PermissionMode.DEFAULT
    cwd = Path(live.perms.cwd)
    sent: dict = {}

    async def ask(kind, payload, session_id):
        sent.update(payload)
        return {"choice": "always"}

    server._ask_client = ask
    approve = await server._approval_callback_for(live)
    args = {"path": str(cwd / ".k3code" / "worktrees" / "sa-1234" / "f1.txt"), "content": "ok"}
    decision = live.perms.decide("write", args)
    assert decision.action == "ask" and not decision.always_ask
    answer = await approve("write", args, decision)
    assert sent["choices"] == ["once", "deny"] and sent["allow_session"] is False and sent["pattern"] == ""
    assert answer.choice == "once" and live.perms.session_rules == []
    assert not (cwd / ".k3code" / "config.yaml").exists()
    await server.close()


# ── FIX 7: a call spelled the way Claude Code spells it is still named in the failed-calls list ───────────────────


def test_describe_call_names_the_file_of_a_call_with_an_alias_spelling():
    assert describe_call("edit", {"file_path": "/p/a.py", "old_str": "a", "new_str": "b"}) == "edit /p/a.py"
    assert describe_call("read", {"file_path": "/p/a.py"}) == "read /p/a.py"
    assert describe_call("write", {"file_path": "/p/a.py", "content": ""}) == "write /p/a.py"
    assert describe_call("edit", {"path": "/p/a.py"}) == "edit /p/a.py"
    assert describe_call("edit", None) == "edit"
    assert describe_call("bash", {"command": "ls"}) == "bash `ls`"


async def test_the_stop_message_names_the_file_of_a_failed_alias_call(tmp_path):
    missing = tmp_path / "missing.py"
    loop = loop_for(tmp_path, [call("r", "read", file_path=str(missing))], mode=PermissionMode.YOLO)
    loop.max_tool_errors = 1
    await run(loop)
    assert f"- read {missing}:" in loop.turn_messages[-1].content


# ── FIX 8: a failed edit of a huge file does not search it for near matches ────────────────────────────────────────


def test_a_failed_edit_of_a_file_over_a_megabyte_gets_the_plain_no_match_message(tmp_path):
    small, big = tmp_path / "small.py", tmp_path / "big.py"
    small.write_text("".join(f"value_{i} = {i}\n" for i in range(300)))
    big.write_text("".join(f"value_{i} = {i}\n" for i in range(70_000)))
    assert len(big.read_text()) > HINT_MAX_CHARS
    near_miss = {"old_string": "value_5 = 6", "new_string": "x"}  # one line: only the closest-lines search could help
    on_small = plan_edit({"path": str(small), **near_miss})
    on_big = plan_edit({"path": str(big), **near_miss})
    assert "Did you mean" in on_small["error"]
    assert on_big == {"error": "Could not find a match for old_string in the file"}


# ── the ninth pass: the stamps and the project check, once more ─────────────────────────────────────────────────────


async def test_a_write_after_a_hooked_edit_needs_a_fresh_read(tmp_path):
    """What the hook's window let in (a save of the user's) cannot be told from the hook's own rewrite, so the
    whole-file `write` that would replace it unseen needs a read first; an `edit` is not held up (its old_string must
    still match)."""
    f = tmp_path / "a.py"
    f.write_text("x=1\ny=2\n")
    script = [
        call("r", "read", path=str(f)),
        call("e", "edit", path=str(f), old_string="x=1", new_string="x = 10"),
        call("w1", "write", path=str(f), content="whole\n"),
        call("r2", "read", path=str(f)),
        call("w2", "write", path=str(f), content="whole\n"),
    ]
    loop = loop_for(tmp_path, script, mode=PermissionMode.YOLO)
    loop.hooks = formatter_hook(tmp_path, f)
    results = await run(loop)
    assert "Edited: 1 replacement" in results["e"]
    assert "Refused: a hook rewrote" in results["w1"] and "read it before you write it whole" in results["w1"]
    assert "Wrote" in results["w2"] and f.read_text().startswith("whole\n")


async def test_a_hook_that_does_not_match_the_edit_does_not_hold_up_a_whole_file_write(tmp_path):
    """Only a hook that ran for the edit can have rewritten the file: a PostToolUse hook for bash changes nothing."""
    f = tmp_path / "a.py"
    f.write_text("x=1\ny=2\n")
    script = [
        call("r", "read", path=str(f)),
        call("e", "edit", path=str(f), old_string="x=1", new_string="x = 10"),
        call("w", "write", path=str(f), content="whole\n"),
    ]
    loop = loop_for(tmp_path, script, mode=PermissionMode.YOLO)
    loop.hooks = userhooks.HookRunner([userhooks.Hook("PostToolUse", "true", "bash")], cwd=tmp_path)
    results = await run(loop)
    assert "Wrote" in results["w"] and f.read_text() == "whole\n"


def test_the_stamps_kept_across_a_resume_are_the_newest_not_the_first(tmp_path):
    stamps = FileStamps()
    files = []
    for i in range(FileStamps.KEEP + 50):
        f = tmp_path / f"f{i}.txt"
        f.write_text("x")
        files.append(f)
        stamps.record(f)
    stamps.record(files[0])  # the file worked on last: first inserted, but newest
    kept = stamps.export()
    assert len(kept) == FileStamps.KEEP
    assert str(files[0]) in kept and str(files[1]) not in kept
    fresh = FileStamps()
    fresh.load(kept)
    assert fresh.refusal("edit", files[0]) is None
    stamps.clear()
    assert stamps.export() == {} and stamps.refusal("edit", files[0]) is not None


def test_a_link_followed_by_dotdot_is_judged_by_the_file_it_reaches(tmp_path):
    project, outside = tmp_path / "project", tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    project.mkdir()
    (project / "link").symlink_to(outside / "sub")
    (outside / "x.txt").write_text("secret")
    (project / "x.txt").write_text("mine")
    lexical = project / "link" / ".." / "x.txt"  # normpath says project/x.txt; the kernel reaches outside/x.txt
    assert not inside_roots(str(lexical), project)
    assert inside_roots(project / "x.txt", project)


async def test_clear_forgets_what_the_session_had_read(tmp_path):
    from k3code.commands.builtin import _ClearCommand
    from k3code.gateway.sessions import StoredSession

    stamps_meta = {str(tmp_path / "a.py"): [1, 2]}

    class Store:
        """The row as the database holds it: a copy with the stamps in its meta."""

        saved: list[dict] = []

        def get(self, _sid: str) -> StoredSession:
            return StoredSession(session_id="s1", cwd=str(tmp_path), meta={"file_stamps": dict(stamps_meta)})

        def save(self, sess: StoredSession) -> None:
            self.saved.append(dict(sess.meta))

    stored = StoredSession(session_id="s1", cwd=str(tmp_path), meta={"file_stamps": dict(stamps_meta)})
    f = tmp_path / "a.py"
    f.write_text("x")
    live = SimpleNamespace(
        messages=[],
        shell_records=[],
        stored=stored,
        streaming=False,
        reads={},
        pinned_reads=set(),
        elided=set(),
        elide_notes={},
        refresh_prompt_inputs=lambda: None,
        file_stamps=FileStamps(),
    )
    live.file_stamps.record(f)
    store = Store()
    await _ClearCommand().handle(SimpleNamespace(sessions={"s1": live}, store=store), "s1", "")
    assert live.file_stamps.refusal("edit", f) is not None
    assert "file_stamps" not in live.stored.meta
    assert store.saved and all("file_stamps" not in meta for meta in store.saved)  # what is written has none


def blocking_formatter(tmp_path, target: Path) -> userhooks.HookRunner:
    """`eslint --fix || exit 2`: fixes the file and reports on stderr, which blocks (the usual lint-hook shape)."""
    hook = tmp_path / "lint.sh"
    hook.write_text(f"#!/bin/sh\nprintf '# fixed\\n' >> {target}\necho 'lint fixed 1 problem' >&2\nexit 2\n")
    hook.chmod(0o755)
    return userhooks.HookRunner([userhooks.Hook("PostToolUse", str(hook), "edit|write")], cwd=tmp_path)


async def test_a_blocking_hook_that_rewrote_the_file_does_not_make_the_next_edit_stale(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x=1\ny=2\n")
    loop = loop_for(tmp_path, two_edits(f), mode=PermissionMode.YOLO)
    loop.hooks = blocking_formatter(tmp_path, f)
    results = await run(loop)
    assert "Edited: 1 replacement" in results["e1"] and "lint fixed 1 problem" in results["e1"]
    assert "Edited: 1 replacement" in results["e2"], results["e2"]


async def test_a_hook_that_leaves_the_file_alone_does_not_hold_up_a_whole_file_write(tmp_path):
    """A logging or audit hook that matches the edit (or fails) says nothing about the file: no false 'rewrote it'."""
    f = tmp_path / "a.py"
    f.write_text("x=1\ny=2\n")
    hook = tmp_path / "log.sh"
    hook.write_text("#!/bin/sh\nexit 1\n")  # matches, changes nothing, and errors
    hook.chmod(0o755)
    script = [
        call("r", "read", path=str(f)),
        call("e", "edit", path=str(f), old_string="x=1", new_string="x = 10"),
        call("w", "write", path=str(f), content="whole\n"),
    ]
    loop = loop_for(tmp_path, script, mode=PermissionMode.YOLO)
    loop.hooks = userhooks.HookRunner([userhooks.Hook("PostToolUse", str(hook), "*")], cwd=tmp_path)
    results = await run(loop)
    assert "Wrote" in results["w"] and f.read_text() == "whole\n"


def test_a_file_a_hook_rewrote_is_not_carried_across_a_resume(tmp_path):
    kept, rewritten = tmp_path / "kept.py", tmp_path / "rewritten.py"
    for f in (kept, rewritten):
        f.write_text("x")
    stamps = FileStamps()
    stamps.record(kept)
    stamps.record(rewritten, absorbed=True)
    exported = stamps.export()
    assert str(kept) in exported and str(rewritten) not in exported
    fresh = FileStamps()
    fresh.load(exported)
    assert fresh.refusal("write", kept) is None and "not read" in (fresh.refusal("write", rewritten) or "")
