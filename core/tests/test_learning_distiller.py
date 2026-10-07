import json

from k3code.autonomy.proposals import ProposalStore
from k3code.config import Mem0Config, Settings
from k3code.learning import distiller
from k3code.learning.decisions import DecisionLog
from learn_helpers import FakeCaller


def seed(log: DecisionLog) -> None:
    for i in range(3):
        log.record("model_switch", subject="m-x -> m-y", choice="m-y",
                   detail={"from": "m-x", "to": "m-y", "task_kind": "review", "to_tier": "strong"})
        log.record("plan", subject="exit_plan", choice="deny", detail={"has_verification": False})
        log.record("proposal", subject="t", choice="dismiss", detail={"kind": "improvement"})
    for i in range(5):
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
    prefs = await distiller.distill(log, home=tmp_path, user_md=md, config=cfg,
                                    mem0_post=lambda u, b, h: sent.append((u, b, h)))
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
