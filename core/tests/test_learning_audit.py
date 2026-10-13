"""Audit fixes in k3code.learning: user-owned files survive bad text, no history loss, bounded work."""

from __future__ import annotations

import json
import os

import pytest

from k3code.autonomy.proposals import ProposalStore
from k3code.learning import distiller, gotchas
from k3code.learning.decisions import DecisionLog
from k3code.learning.distiller import Preference
from learn_helpers import FakeCaller

LONE = "\ud83d"  # a lone high surrogate: json.loads accepts it, UTF-8 cannot encode it
BAD_NAME = os.fsdecode(b"/tmp/caf\xe9.txt")  # a non-UTF-8 file name decodes to a lone surrogate


# ── H1: a surrogate must not empty a user-owned file ──


def test_user_md_keeps_its_content_when_a_preference_cannot_be_encoded(tmp_path):
    md = tmp_path / "USER.md"
    md.write_text("# Me\nI like tabs.\n")
    distiller.write_auto_section(md, [Preference(f"prefers small edits {LONE}", 0.9, 5, "undo")])
    text = md.read_text(encoding="utf-8")
    assert "I like tabs." in text and "prefers small edits" in text
    distiller.add_user_line(md, f"use pnpm {LONE}")
    assert "I like tabs." in md.read_text(encoding="utf-8") and "use pnpm" in md.read_text(encoding="utf-8")
    assert not [p for p in tmp_path.iterdir() if p.name != "USER.md"]  # no temp file left behind


def test_user_md_is_not_emptied_when_the_write_itself_fails(tmp_path, monkeypatch):
    md = tmp_path / "USER.md"
    md.write_text("# Me\nI like tabs.\n")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        distiller.write_auto_section(md, [Preference("a", 0.9, 5, "k")])
    monkeypatch.undo()
    assert md.read_text() == "# Me\nI like tabs.\n"
    assert [p.name for p in tmp_path.iterdir()] == ["USER.md"]


async def test_a_model_reply_with_a_lone_surrogate_does_not_wipe_user_md_or_preferences(tmp_path):
    log = DecisionLog(tmp_path)
    _seed_pref(log)
    md = tmp_path / "USER.md"
    md.write_text("# Me\nI like tabs.\n")
    caller = FakeCaller(f'[{{"key": "{PREF}", "text": "prefers small edits \\ud83d"}}]')
    await distiller.distill(log, home=tmp_path, user_md=md, caller=caller)
    assert "I like tabs." in md.read_text(encoding="utf-8")
    assert "prefers small edits" in md.read_text(encoding="utf-8")
    prefs = json.loads((tmp_path / "learning" / "preferences.json").read_text(encoding="utf-8"))
    assert "prefers small edits" in prefs[PREF]["text"]
    assert oct((tmp_path / "learning" / "preferences.json").stat().st_mode & 0o777) == "0o600"
    assert oct(md.stat().st_mode & 0o777) == "0o600"


def test_a_non_utf8_file_name_in_a_gotcha_keeps_the_other_gotchas(tmp_path):
    path = tmp_path / "projects" / "p" / "gotchas.md"
    gotchas.append_gotcha(path, "bash: first")
    gotchas.append_gotcha(path, f"bash: cat {BAD_NAME} — `cat {BAD_NAME}.bak` worked instead")
    text = path.read_text(encoding="utf-8")
    assert "- bash: first" in text and "worked instead" in text


def test_the_decision_log_keeps_a_row_whose_text_has_a_lone_surrogate(tmp_path):
    log = DecisionLog(tmp_path)
    rid = log.record("tool_error", cwd=f"/w/{BAD_NAME}", subject=f"cat {BAD_NAME}", choice="x", detail={"e": BAD_NAME})
    log.update_detail(rid, followup=f"`cat {BAD_NAME}.bak` worked instead")
    (row,) = log.query("tool_error")
    assert "caf" in row["subject"] and "caf" in row["detail"]["e"] and "worked instead" in row["detail"]["followup"]


def test_a_proposal_with_a_lone_surrogate_is_stored(tmp_path):
    store = ProposalStore(tmp_path)
    p = store.add("improvement", f"clean up {BAD_NAME}", "do it")
    assert p is not None and "caf" in store.get(p.id).text


# ── M3: experiments.json ──


def _xp(tmp_path, monkeypatch):
    from k3code.learning import optimizer
    from learn_helpers import FakeClock

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    return optimizer.Experiments(tmp_path, FakeClock())


def test_a_torn_experiments_file_is_kept_and_never_saved_over_as_empty(tmp_path, monkeypatch, caplog):
    xp = _xp(tmp_path, monkeypatch)
    xp.path.parent.mkdir(parents=True)
    torn = '[{"id": "x1", "title": "t", "status": "active", "sessions_done": 0, "target_sessions": 1, "star'
    xp.path.write_text(torn)
    assert xp.all() == []
    with caplog.at_level("WARNING"):
        assert xp.session_done(lambda since: {}) == []  # it used to _save([]): the history gone for good
    assert xp.path.read_text() == torn
    assert (xp.path.parent / "experiments.json.bad").read_text() == torn
    assert "unreadable" in caplog.text


def test_a_new_experiment_replaces_a_torn_file_but_the_bad_copy_stays(tmp_path, monkeypatch):
    from test_learning_optimizer import metrics_for

    xp = _xp(tmp_path, monkeypatch)
    xp.path.parent.mkdir(parents=True)
    xp.path.write_text("[{")
    exp = xp.start({"title": "p", "name": "n", "prompt": "be brief"}, metrics_for(0.1), sessions=1)
    assert [x["id"] for x in xp.all()] == [exp["id"]]
    assert (xp.path.parent / "experiments.json.bad").read_text() == "[{"


def test_experiment_ids_are_not_reused_when_the_file_lost_entries(tmp_path, monkeypatch):
    from test_learning_optimizer import metrics_for

    xp = _xp(tmp_path, monkeypatch)
    for name in ("one", "two"):
        xp.start({"title": name, "name": name, "prompt": f"say {name}"}, metrics_for(0.1), sessions=1)
    assert [x["id"] for x in xp.all()] == ["x1", "x2"]
    xp.path.write_text(json.dumps(xp.all()[1:]))  # x1 is gone from the history: len(items)+1 gave x2 again
    third = xp.start({"title": "three", "name": "three", "prompt": "say three"}, metrics_for(0.1), sessions=1)
    assert third["id"] == "x3"
    xp.path.write_text("[]")  # everything gone, the overlay files stay: their ids stay taken
    assert xp.start({"title": "four", "name": "four", "prompt": "say four"}, metrics_for(0.1), sessions=1)["id"] == "x4"


# ── M13: an overlay file that is not UTF-8 never blocks a prompt ──


def test_a_non_utf8_overlay_file_does_not_break_the_prompt(tmp_path, monkeypatch):
    from k3code.config import Settings
    from k3code.learning import optimizer
    from k3code.prompting import build_system_prompt

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "a.md").write_bytes(b"always say caf\xe9 first\n")
    (tmp_path / "overlays" / "b.md").write_text("be brief\n")
    text = optimizer.overlay_prompt()
    assert "be brief" in text and "caf" in text
    assert "be brief" in build_system_prompt("base", cwd=tmp_path, config=Settings())


# ── M12: a folder of projects is not one big project ──


def test_a_plain_folder_of_many_projects_offers_only_a_few_stacks(tmp_path):
    from k3code.learning import stacks

    for i in range(30):
        (tmp_path / "code" / f"proj{i:02d}").mkdir(parents=True)
        (tmp_path / "code" / f"proj{i:02d}" / "go.mod").write_text("module x\n")
    found = stacks.scan(tmp_path / "code").stacks
    assert 0 < len(found) <= 8


def test_a_repository_with_many_services_still_scans_all_of_them(tmp_path):
    from k3code.learning import stacks

    (tmp_path / "repo" / ".git").mkdir(parents=True)
    for i in range(30):
        (tmp_path / "repo" / f"svc{i:02d}").mkdir()
        (tmp_path / "repo" / f"svc{i:02d}" / "go.mod").write_text("module x\n")
    assert len(stacks.scan(tmp_path / "repo").stacks) == 30


# ── M8: the curator archives the skill it judged ──


def test_accepting_a_stale_card_archives_the_stale_skill_not_the_directory_with_its_name(tmp_path, monkeypatch):
    import time

    from k3code.learning import curator

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    root = tmp_path / "skills"
    now = time.time()
    for dirname, name in (("old-deploy", "deploy"), ("deploy", "deploy-prod")):
        (root / dirname).mkdir(parents=True)
        (root / dirname / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} things\n---\nsteps for {dirname}\n"
        )
    old = now - 90 * 86400
    os.utime(root / "old-deploy" / "SKILL.md", (old, old))
    store = ProposalStore(tmp_path / "p")
    res = curator.curate(store, now=now, stale_days=30, root=root)
    (card,) = [p for p in res["proposals"] if "'deploy' looks stale" in p.text]
    msg = curator.apply(card.payload, root)
    assert "archived" in msg
    assert (root / "deploy" / "SKILL.md").is_file(), "the ACTIVE skill must stay"
    assert (root / "_archive" / "old-deploy" / "SKILL.md").is_file() and not (root / "old-deploy").exists()


def test_a_payload_cannot_archive_outside_the_skills_directory(tmp_path):
    from k3code.learning import curator

    root = tmp_path / "skills"
    root.mkdir()
    (tmp_path / "victim").mkdir()
    for drop in ("../victim", "/", ".", ".."):
        assert "not found" in curator.apply({"op": "archive", "drop": drop}, root)
        assert "not found" in curator.apply({"op": "archive", "drop": "x", "drop_dir": drop}, root)
    assert (tmp_path / "victim").is_dir() and root.is_dir()
    assert "not a plain" in curator.apply(
        {"op": "save", "name": "../evil", "draft": str(tmp_path / "d" / "SKILL.md")}, root
    )


def test_a_legacy_archive_payload_resolves_by_name_only_when_unambiguous(tmp_path):
    from k3code.learning import curator

    root = tmp_path / "skills"
    for dirname, name in (("a", "same"), ("b", "same"), ("c", "unique")):
        (root / dirname).mkdir(parents=True)
        (root / dirname / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nx\n")
    assert "not found" in curator.apply({"op": "archive", "drop": "same"}, root)  # two skills called "same"
    assert (root / "a").is_dir() and (root / "b").is_dir()
    assert "archived" in curator.apply({"op": "archive", "drop": "unique"}, root)
    assert (root / "_archive" / "c").is_dir()


def test_curate_still_proposes_when_the_marks_file_cannot_be_written(tmp_path, monkeypatch):
    import time

    from k3code.learning import curator

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    root = tmp_path / "skills"
    (root / "s").mkdir(parents=True)
    (root / "s" / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nx\n")
    (root / ".curator.json").mkdir()  # a directory where the marks file goes
    res = curator.curate(ProposalStore(tmp_path / "p"), now=time.time() + 90 * 86400, root=root)
    assert [p.payload["drop"] for p in res["proposals"]] == ["s"]


# ── L5: a bad line in the legacy decisions file must not stop the gateway ──


def test_a_malformed_legacy_decisions_line_is_skipped_with_a_warning(tmp_path, caplog):
    good = json.dumps({"ts": 5, "tool": "bash", "pattern": "git status", "choice": "once", "cwd": "", "session": "s"})
    (tmp_path / "decisions.jsonl").write_text(
        "\n".join(
            [good, "[1, 2]", '{"ts": "yesterday", "pattern": "x"}', "not json", '{"ts": null, "pattern": null}', good]
        )
        + "\n"
    )
    with caplog.at_level("WARNING"):
        log = DecisionLog(tmp_path)
    assert log.count("approval") == 3  # the two good lines and the one with nothing but nulls
    assert "line 2 skipped" in caplog.text and "line 3 skipped" in caplog.text
    assert (tmp_path / "decisions.jsonl.migrated").exists()


# ── hub: maintenance, replay recording, permission mining, failures, shutdown ──


async def _hub(tmp_path, monkeypatch, **settings):
    from m1cmd_helpers import git_repo, make_server, new_session

    server, _ = make_server(tmp_path, monkeypatch, **settings)
    repo = git_repo(tmp_path / "repo")
    sid = await new_session(server, repo)
    return server, server.learning, server.live_for(server.store.get(sid))


#: the preference three sessions of leaving model m1 derive (the "undo" kind it used to rest on is gone)
PREF = "avoid:m1:"


def _seed_pref(log, sessions: int = 3, first: int = 0) -> None:
    for i in range(first, first + sessions):
        log.record("model_switch", session=f"s{i}", subject="m2", detail={"from": "m1", "task_kind": ""})


def _seed_undo(hub) -> None:
    _seed_pref(hub.log)


class _SlowCaller(FakeCaller):
    async def complete(self, kind, messages, **kw):
        import asyncio

        await asyncio.sleep(0.05)
        return await super().complete(kind, messages, **kw)


async def test_two_sessions_finishing_together_make_one_maintenance_pass(tmp_path, monkeypatch):
    import asyncio

    server, hub, _ = await _hub(tmp_path, monkeypatch)
    _seed_undo(hub)
    caller = server.model_caller = _SlowCaller(f'[{{"key": "{PREF}", "text": "prefers small edits"}}]')
    a, b = await asyncio.gather(hub.maintenance(), hub.maintenance())
    assert len(caller.calls) == 1, "the second pass read the state before the first one stamped it"
    assert sorted(map(bool, (a, b))) == [True, True]  # one did the work, the other says it skipped
    await server.close()


async def test_a_failed_maintenance_pass_is_not_tried_again_at_every_turn_end(tmp_path, monkeypatch):
    import pytest

    from k3code.learning import curator

    server, hub, _ = await _hub(tmp_path, monkeypatch)
    _seed_undo(hub)
    caller = server.model_caller = FakeCaller(f'[{{"key": "{PREF}", "text": "prefers small edits"}}]')
    monkeypatch.setattr(curator, "curate", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        await hub.maintenance()
    calls = len(caller.calls)
    for _ in range(3):  # three more turn ends: no pass, no model call
        assert not (await hub.maintenance()).get("preferences")
    assert len(caller.calls) == calls
    hub.clock = lambda t=hub.clock(): t + 2 * 3600  # an hour later it may try again
    with pytest.raises(ZeroDivisionError):
        await hub.maintenance()
    await server.close()


async def test_a_state_time_in_the_future_does_not_stop_maintenance_for_good(tmp_path, monkeypatch):
    server, hub, _ = await _hub(tmp_path, monkeypatch)
    _seed_undo(hub)
    server.model_caller = FakeCaller("[]")
    hub._set_state(distilled=hub.clock() + 400 * 86400)  # a clock that was once wrong
    done = await hub.maintenance()
    assert "preferences" in done
    assert hub._state()["distilled"] <= hub.clock() + 1
    await server.close()


async def test_an_exception_in_a_learning_task_is_logged(tmp_path, monkeypatch, caplog):
    server, hub, _ = await _hub(tmp_path, monkeypatch)

    async def boom():
        raise RuntimeError("kaboom")

    with caplog.at_level("WARNING"):
        hub.spawn(boom())
        await hub.drain()
    assert "learning task failed" in caplog.text and "kaboom" in caplog.text
    await server.close()


async def test_closing_the_server_cancels_learning_tasks_and_closes_the_decision_log(tmp_path, monkeypatch):
    import asyncio
    import sqlite3

    import pytest

    server, hub, _ = await _hub(tmp_path, monkeypatch)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def forever():
        started.set()
        await gate.wait()

    before = set(hub._tasks)
    hub.spawn(forever())
    await started.wait()
    (task,) = hub._tasks - before
    await server.close()
    assert task.cancelled() and not hub._tasks
    with pytest.raises(sqlite3.ProgrammingError):
        hub.log.count()


def _messages():
    from k3code.providers.types import Message

    return [
        Message(role="system", content="s"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="yo"),
    ]


async def test_replay_records_are_kept_while_learning_is_on_whether_or_not_the_optimizer_is(tmp_path, monkeypatch):
    """/optimizer run is an analysis of the records while the optimizer is off: they must exist by then."""
    server, hub, live = await _hub(tmp_path, monkeypatch)
    hub.record_turn_replay(live, _messages(), "done", "main", "interactive_turn", "t1", history_len=0)
    assert len(hub.replays.load()) == 1
    server.config.learning = {"optimizer": {"enabled": True}}
    hub.record_turn_replay(live, _messages(), "done", "main", "interactive_turn", "t2", history_len=0)
    assert len(hub.replays.load()) == 2
    server.config.learning = {"enabled": False}
    hub.record_turn_replay(live, _messages(), "done", "main", "interactive_turn", "t3", history_len=0)
    assert len(hub.replays.load()) == 2  # learning itself off: nothing is recorded
    await server.close()


async def test_recorded_skill_lines_follow_the_configured_skill_prompt_limit(tmp_path, monkeypatch):
    server, hub, live = await _hub(tmp_path, monkeypatch, learning={"optimizer": {"enabled": True}})
    sroot = tmp_path / "skills"
    for i in range(10):
        (sroot / f"s{i}").mkdir(parents=True)
        (sroot / f"s{i}" / "SKILL.md").write_text(f"---\nname: s{i}\ndescription: skill number {i}\n---\nbody\n")
    server.config.skills.roots = [str(sroot)]
    server.config.context = {"skill_prompt_limit": 3}
    hub.record_turn_replay(live, _messages(), "done", "main", "interactive_turn", "t1", history_len=0)
    (rec,) = hub.replays.load()
    assert len(rec["skill_lines"]) == 3
    await server.close()


async def test_the_optimizer_replays_the_newest_turns_in_a_worker_thread(tmp_path, monkeypatch):
    import threading

    from k3code.learning import replay
    from test_learning_replay import turn

    server, hub, _ = await _hub(tmp_path, monkeypatch, learning={"optimizer": {"enabled": True, "min_sessions": 0}})
    for i in range(600):
        hub.replays.append(turn(i))
    seen: dict = {}

    async def fake_candidates(context, turns):
        seen["thread"] = threading.current_thread()
        seen["turns"] = [t["turn"] for t in turns]
        return []

    monkeypatch.setattr(replay, "token_candidates", fake_candidates)
    await hub.run_optimizer()
    assert seen["thread"] is not threading.main_thread(), "the replay blocked the event loop"
    assert len(seen["turns"]) == 500 and seen["turns"][-1] == "t599" and seen["turns"][0] == "t100"
    await server.close()


def test_the_before_baseline_is_replayed_once_for_all_candidates(monkeypatch):
    import asyncio

    from k3code.learning import replay
    from test_learning_replay import turn

    calls = {"n": 0}
    real = replay._replay_turn

    async def counting(provider, t, cand):
        calls["n"] += 1
        return await real(provider, t, cand)

    monkeypatch.setattr(replay, "_replay_turn", counting)
    turns = [turn(i, tool_chars=9000, memory=12_000, skills=[80] * 30) for i in range(10)]
    cands = asyncio.run(replay.token_candidates({}, turns))
    assert len(cands) == 3
    assert calls["n"] == 10 * (1 + 3), "the baseline is the same for every candidate: replay it once"


async def test_permissions_are_mined_in_a_worker_thread_and_only_when_approvals_changed(tmp_path, monkeypatch):
    import threading

    from k3code.learning import permrules

    server, hub, live = await _hub(tmp_path, monkeypatch)
    hub._set_state(distilled=hub.clock())
    seen: list = []
    real = permrules.mine

    def spy(log, **kw):
        seen.append(threading.current_thread())
        return real(log, **kw)

    monkeypatch.setattr(permrules, "mine", spy)
    for status in ("error", "error", "error"):
        await hub.turn_finished(live, status)
    assert len(seen) == 1 and seen[0] is not threading.main_thread()
    hub.log.record("approval", cwd=str(live.perms.cwd), subject="echo hi", choice="once", detail={"tool": "bash"})
    await hub.turn_finished(live, "error")
    assert len(seen) == 2
    await hub.turn_finished(live, "error")
    assert len(seen) == 2
    await server.close()


def test_the_proposal_file_is_not_read_again_for_every_candidate(tmp_path, monkeypatch):
    from pathlib import Path

    store = ProposalStore(tmp_path)
    reads = {"n": 0}
    real = Path.read_text

    def spy(self, *a, **kw):
        reads["n"] += self.name == "proposals.jsonl"
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", spy)
    for i in range(40):
        assert store.add("improvement", f"idea {i}", "x") is not None
    for i in range(40):
        assert store.add("improvement", f"idea {i}", "x") is None  # the duplicate check needs no read either
    assert reads["n"] <= 1


async def test_the_last_failure_memory_is_bounded_and_keeps_only_what_the_hint_needs(tmp_path, monkeypatch):
    from types import SimpleNamespace

    server, hub, live = await _hub(tmp_path, monkeypatch)

    def sess(i):
        return SimpleNamespace(session_id=f"s{i}", perms=SimpleNamespace(cwd=live.perms.cwd), background=False)

    def failure(sig="boom"):
        return SimpleNamespace(signature=sig, error_class="not_found", first_line=sig)

    big = "x" * 100_000
    for i in range(300):
        call = SimpleNamespace(name="write", arguments={"path": "p.txt", "content": big})
        hub.tool_outcome(sess(i), call, {}, failure())
    held = sum(len(repr(v)) for v in hub._last_failure.values())
    assert len(hub._last_failure) <= 256 and held < 1_000_000
    # the retry that changed only the path is still recognised as such, with the long content compared by digest
    retry = SimpleNamespace(name="write", arguments={"path": "q.txt", "content": big})
    hub.tool_outcome(sess(299), retry, {}, None)
    (row,) = [r for r in hub.log.query("tool_error", actor=None) if r["session"] == "s299"]
    assert row["detail"]["followup"] == "worked with different path"
    hub.tool_outcome(
        sess(298), SimpleNamespace(name="write", arguments={"path": "p.txt", "content": big}), {}, failure()
    )
    hub.forget_session("s298")
    assert not [k for k in hub._last_failure if k[0] == "s298"]
    await server.close()


# ── L2: the daily polish and mem0 ──


async def test_unchanged_preferences_are_not_polished_again_the_next_day(tmp_path):
    log = DecisionLog(tmp_path)
    _seed_pref(log)
    md = tmp_path / "USER.md"
    caller = FakeCaller(
        f'[{{"key": "{PREF}", "text": "prefers small edits"}}]', f'[{{"key": "{PREF}", "text": "likes tiny diffs"}}]'
    )
    first = await distiller.distill(log, home=tmp_path, user_md=md, caller=caller)
    second = await distiller.distill(log, home=tmp_path, user_md=md, caller=caller)
    assert len(caller.calls) == 1
    assert [p.text for p in first] == [p.text for p in second] == ["prefers small edits"]
    _seed_pref(log, first=3)  # three more sessions of evidence: it is worth asking again
    await distiller.distill(log, home=tmp_path, user_md=md, caller=caller)
    assert len(caller.calls) == 2


def test_a_reworded_preference_is_not_posted_to_mem0_as_a_new_memory(tmp_path):
    from k3code.config import Mem0Config, Settings

    cfg = Settings(mem0=Mem0Config(url="http://mem0.test", api_key_env="NOPE", agent_id="a"))
    sent: list[str] = []

    def post(url, body, headers):
        sent.append(body["messages"][0]["content"])

    posted = tmp_path / "posted.json"
    pref = Preference("prefers small edits", 0.7, 3, "undo")
    assert distiller.store_mem0(cfg, [pref], post, posted) == 1
    reworded = Preference("likes tiny diffs", 0.8, 4, "undo")
    assert distiller.store_mem0(cfg, [reworded], post, posted) == 0
    assert len(sent) == 1
    # what an older version recorded (the text's hash) still counts as posted
    old = tmp_path / "old.json"
    m = cfg.mem0
    old.write_text(json.dumps([distiller._posted_key(m, "avoids model x")]))
    assert distiller.store_mem0(cfg, [Preference("avoids model x", 0.9, 5, "avoid:x")], post, old) == 0


def test_a_gotchas_file_that_is_not_utf8_does_not_break_the_prompt(tmp_path):
    path = gotchas.gotchas_path("git:example.com/o/r", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"- bash: cat caf\xe9 failed\n")
    assert any("caf" in e for e in gotchas._entries(path))


async def test_a_non_utf8_file_name_in_a_tool_error_is_learned_and_reaches_no_client_as_unsendable_text(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    server, hub, live = await _hub(tmp_path, monkeypatch)

    def failure():
        return SimpleNamespace(signature=f"cat {BAD_NAME}: no such file", error_class="not_found", first_line="x")

    def call(cmd):
        return SimpleNamespace(name="bash", arguments={"command": cmd})

    assert hub.tool_outcome(live, call(f"cat {BAD_NAME}"), {}, failure()) is None
    hub.tool_outcome(live, call("cat /tmp/other.txt"), {}, None)  # the retry that worked
    reminder = hub.tool_outcome(live, call(f"cat {BAD_NAME}"), {}, failure())  # second sighting: learned
    assert reminder is not None and "no such file" in reminder
    reminder.encode("utf-8")  # a lone surrogate would raise here, and in the model request it is sent in
    pid = hub.log.project_for(str(live.perms.cwd))
    auto = gotchas.auto_gotchas_path(pid, hub.home).read_text(encoding="utf-8")
    assert "no such file" in auto and "worked instead" in auto
    for frame in server._frames:  # cards and notifications as the clients get them
        frame.encode("utf-8")
    assert any("proposal.show" in f for f in server._frames)
    cards = [p for p in hub.store.all() if p.kind == "project_gotcha"]
    assert cards and all(p.text.encode("utf-8") for p in cards)
    await server.close()


async def test_the_session_review_does_not_run_for_unattended_turns(tmp_path, monkeypatch):
    from k3code.learning import review

    server, hub, live = await _hub(tmp_path, monkeypatch)
    hub._set_state(distilled=hub.clock())
    live.stored.messages = [
        m for i in range(12) for m in ({"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": "a"})
    ]
    calls: list[int] = []

    async def fake_review(*args, **kw):
        calls.append(1)
        return {"proposals": []}

    monkeypatch.setattr(review, "review_session", fake_review)
    live.background = True  # a loop tick, a cron run, an automation
    await hub.turn_finished(live, "done")
    assert calls == []
    live.background = False
    await hub.turn_finished(live, "done")
    assert calls == [1]
    await server.close()
