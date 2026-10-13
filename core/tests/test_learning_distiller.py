import json

from k3code.autonomy.proposals import ProposalStore
from k3code.config import Mem0Config, Settings
from k3code.learning import distiller
from k3code.learning.decisions import DecisionLog
from learn_helpers import FakeCaller, FakeClock


def seed(log: DecisionLog) -> None:
    for _ in range(3):
        log.record(
            "model_switch",
            subject="m-x -> m-y",
            choice="m-y",
            detail={"from": "m-x", "to": "m-y", "task_kind": "review", "to_tier": "strong"},
        )
        log.record("plan", subject="exit_plan", choice="deny", detail={"has_verification": False})
        log.record("proposal", subject="t", choice="dismiss", detail={"kind": "improvement"})
    for _ in range(5):
        log.record("approval", subject="npm test *", choice="once", detail={"tool": "bash"})


async def test_auto_section_rewritten_user_text_preserved(tmp_path):
    log = DecisionLog(tmp_path)
    seed(log)
    md = tmp_path / "memory" / "USER.md"
    md.parent.mkdir(parents=True)
    md.write_text("# Me\nI like tabs.\n\n## Learned preferences (auto)\n- stale old line\n\n## My notes\nkeep this\n")
    prefs = await distiller.distill(log, home=tmp_path, user_md=md)
    text = md.read_text()
    assert "I like tabs." in text and "## My notes\nkeep this" in text and "stale old line" not in text
    assert "avoids model m-x for review tasks" in text
    assert "rejects plans without verification steps" in text
    assert "always wants tests run" in text
    # second run replaces, never duplicates
    await distiller.distill(log, home=tmp_path, user_md=md)
    assert md.read_text().count("## Learned preferences (auto)") == 1
    data = json.loads((tmp_path / "learning" / "preferences.json").read_text())
    entry = data["avoid:m-x:review"]
    assert entry["evidence"] == 3 and 0 < entry["confidence"] <= 1
    assert prefs and distiller.load_preferences(tmp_path)[0].confidence >= entry["confidence"]


async def test_creates_section_in_missing_file(tmp_path):
    log = DecisionLog(tmp_path)
    seed(log)
    md = tmp_path / "USER.md"
    await distiller.distill(log, home=tmp_path, user_md=md)
    assert distiller.read_auto_section(md)


async def test_mem0_stored_per_preference_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM0_KEY", "super-secret-key-value")
    log = DecisionLog(tmp_path)
    seed(log)
    log.record("approval", subject="deploy --token sk-abcdefghijklmnop", choice="once", detail={"tool": "bash"})
    sent: list[tuple[str, dict, dict]] = []
    cfg = Settings(mem0=Mem0Config(url="http://mem0.test", api_key_env="MEM0_KEY", agent_id="agent-7"))
    md = tmp_path / "USER.md"
    prefs = await distiller.distill(
        log, home=tmp_path, user_md=md, config=cfg, mem0_post=lambda u, b, h: sent.append((u, b, h))
    )
    assert len(sent) == len(prefs) > 0
    assert sent[0][0] == "http://mem0.test/memories" and sent[0][1]["agent_id"] == "agent-7"
    blob = md.read_text() + (tmp_path / "learning" / "preferences.json").read_text() + json.dumps(sent[0][1])
    assert "sk-abcdefgh" not in blob and "super-secret-key-value" not in blob


async def test_no_mem0_when_unconfigured(tmp_path):
    log = DecisionLog(tmp_path)
    seed(log)
    assert distiller.store_mem0(Settings(), distiller.derive(log)[0], lambda *a: 1 / 0) == 0


async def test_cheap_tier_polish_and_fallback(tmp_path):
    log = DecisionLog(tmp_path)
    seed(log)
    caller = FakeCaller(json.dumps([{"key": "avoid:m-x:review", "text": "Do not use m-x for reviews"}]))
    prefs = await distiller.distill(log, home=tmp_path, user_md=tmp_path / "U.md", caller=caller)
    assert any(p.text == "Do not use m-x for reviews" for p in prefs)
    bad = await distiller.distill(log, home=tmp_path, user_md=tmp_path / "U2.md", caller=FakeCaller("garbage"))
    assert any(p.text == "avoids model m-x for review tasks" for p in bad)


async def test_model_switches_propose_task_tier_override(tmp_path):
    log = DecisionLog(tmp_path)
    seed(log)
    store = ProposalStore(tmp_path)
    await distiller.distill(log, home=tmp_path, user_md=tmp_path / "U.md", proposals=store)
    (p,) = [x for x in store.all() if x.kind == "optimizer"]
    assert p.payload == {"task_tiers": {"review": "strong"}}
    await distiller.distill(log, home=tmp_path, user_md=tmp_path / "U.md", proposals=store)
    assert len([x for x in store.all() if x.kind == "optimizer"]) == 1  # latched


async def test_mem0_writes_do_not_block_the_event_loop_thread(tmp_path):
    """store_mem0 does blocking HTTP (10 s timeout per preference); run on the loop it froze every session."""
    import threading

    log = DecisionLog(tmp_path)
    seed(log)
    threads: list[threading.Thread] = []
    cfg = Settings(mem0=Mem0Config(url="http://mem0.test", api_key_env="NOPE", agent_id="a"))
    await distiller.distill(
        log,
        home=tmp_path,
        user_md=tmp_path / "USER.md",
        config=cfg,
        mem0_post=lambda u, b, h: threads.append(threading.current_thread()),
    )
    assert threads and all(t is not threading.main_thread() for t in threads)


# ── the signal behind a permanent preference: windowed, per session, per project ──


def _keys(log: DecisionLog, **kw) -> set[str]:
    return {p.key for p in distiller.derive(log, **kw)[0]}


def test_config_rollbacks_do_not_become_a_preference(tmp_path):
    """The only 'undo' row is /config rollback; it said 'frequently rolls back changes' about code edits."""
    log = DecisionLog(tmp_path)
    for i in range(6):
        log.record("undo", session=f"s{i}", subject="config.yaml", choice="config-rollback")
    prefs, _ = distiller.derive(log)
    assert not prefs and not any("roll" in p.text for p in prefs)


async def test_distill_ignores_decisions_older_than_the_window(tmp_path):
    clock = FakeClock()
    log = DecisionLog(tmp_path, clock)
    for i in range(3):
        log.record("interrupt", session=f"s{i}", subject="bash", choice="stop")
    clock.advance((distiller.PREFERENCE_WINDOW_DAYS + 1) * 86400)
    assert await distiller.distill(log, home=tmp_path, user_md=tmp_path / "U.md") == []
    for i in range(3):  # the same habit again, inside the window
        log.record("interrupt", session=f"t{i}", subject="bash", choice="stop")
    prefs = await distiller.distill(log, home=tmp_path, user_md=tmp_path / "U.md")
    assert [p.key for p in prefs] == ["interrupt:bash"]


def test_repeats_inside_one_session_are_one_piece_of_evidence(tmp_path):
    log = DecisionLog(tmp_path)
    for _ in range(9):
        log.record("interrupt", session="s1", subject="bash", choice="stop")
        log.record("scope", session="s1", subject="override", choice="small")
    assert _keys(log) == set()
    for i in (2, 3):
        log.record("interrupt", session=f"s{i}", subject="bash", choice="stop")
    (pref,) = distiller.derive(log)[0]
    assert pref.key == "interrupt:bash" and pref.evidence == 3


def test_a_habit_needs_one_project_not_one_stray_row_in_each(tmp_path):
    log = DecisionLog(tmp_path)
    for i, project in enumerate(("git:a", "git:b", "git:c")):
        log.record("interrupt", session=f"s{i}", project=project, subject="bash", choice="stop")
    assert _keys(log) == set()
    for i in (3, 4):
        log.record("interrupt", session=f"s{i}", project="git:a", subject="bash", choice="stop")
    (pref,) = distiller.derive(log)[0]
    assert pref.key == "interrupt:bash" and pref.evidence == 3


def test_proposal_rate_is_taken_per_project_and_contradictions_are_dropped(tmp_path):
    """9 accepts in one project and 3 dismissals in another blend to 75% ("welcomes"), which holds in neither."""
    log = DecisionLog(tmp_path)
    for i in range(9):
        log.record("proposal", session=f"a{i}", project="git:a", choice="accept", detail={"kind": "improvement"})
    for i in range(3):
        log.record("proposal", session=f"b{i}", project="git:b", choice="dismiss", detail={"kind": "improvement"})
        log.record("proposal", session=f"c{i}", project="git:c", choice="dismiss", detail={"kind": "docs"})
    assert _keys(log) == {"prop-:docs"}


def test_model_switches_in_three_sessions_of_one_project_still_re_tier(tmp_path):
    log = DecisionLog(tmp_path)
    for i in range(3):
        log.record(
            "model_switch",
            session=f"s{i}",
            project="git:a",
            detail={"from": "m-x", "to": "m-y", "task_kind": "review", "to_tier": "strong"},
        )
    prefs, suggestions = distiller.derive(log)
    assert [p.key for p in prefs] == ["avoid:m-x:review"]
    assert suggestions == [{"model": "m-x", "task_kind": "review", "tier": "strong", "evidence": 3}]
