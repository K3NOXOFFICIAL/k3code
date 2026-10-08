import json

from k3code.autonomy.proposals import ProposalStore
from k3code.learning import curator, distiller, review
from k3code.skills import discover
from learn_helpers import FakeCaller, FakeClock

REPLY = json.dumps({
    "facts": ["tests run with `uv run pytest -q`", "api_key=hunter2 is used"],
    "skills": [{"name": "release-notes", "description": "write release notes",
                "body": "1. list commits\n2. group by type"}, {"name": "Bad Name", "description": "x", "body": "y"}],
})


def convo(n):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(n * 2)]


async def test_short_sessions_skipped(tmp_path):
    caller = FakeCaller(REPLY)
    r = await review.review_session(caller, convo(3), store=ProposalStore(tmp_path), cwd=tmp_path, min_turns=6)
    assert r["skipped"] and not caller.calls


async def test_review_creates_drafts_facts_then_proposal_and_accept_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "K3CODE.md").write_text("# Mine\nhand written\n")
    store = ProposalStore(tmp_path)
    sent = []
    from k3code.config import Mem0Config, Settings

    cfg = Settings(mem0=Mem0Config(url="http://m"))
    r = await review.review_session(FakeCaller(REPLY), convo(8), store=store, cwd=tmp_path / "repo", config=cfg,
                                    min_turns=6, mem0_post=lambda u, b, h: sent.append(b))
    draft = tmp_path / "home" / "skills" / "_drafts" / "release-notes" / "SKILL.md"
    assert draft.is_file() and "group by type" in draft.read_text()
    assert not (tmp_path / "home" / "skills" / "Bad Name").exists()
    mem = (tmp_path / "repo" / "K3CODE.md").read_text()
    assert "hand written" in mem and "uv run pytest -q" in mem and "hunter2" not in mem
    assert len(sent) == 1  # the secret-bearing fact was dropped before mem0
    (p,) = r["proposals"]
    assert p.kind == "skill" and "release-notes" in p.text
    # drafts are not live skills until accepted
    assert discover(tmp_path, None) == []
    curator.apply(p.payload)
    assert [s.name for s in discover(tmp_path)] == ["release-notes"]
    # same skill is not proposed twice
    r2 = await review.review_session(FakeCaller(REPLY), convo(8), store=store, cwd=tmp_path / "repo", min_turns=6)
    assert r2["proposals"] == []


def test_curator_dedup_stale_and_archive(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("K3CODE_HOME", str(home))
    clock = FakeClock()

    def skill(name, desc, body):
        d = home / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")

    body = "run the linter then fix every warning and rerun the linter until clean"
    skill("lint-fix", "fix lint warnings", body)
    skill("lint-repair", "fix lint warnings", body)
    skill("deploy", "deploy the service to production", "ssh and restart unrelated deployment steps")
    skill("flaky", "something flaky", "does a thing that keeps failing here")
    import os

    old = clock() - 40 * 86400
    for n in ("deploy", "lint-fix", "lint-repair"):
        os.utime(home / "skills" / n / "SKILL.md", (old, old))
    os.utime(home / "skills" / "flaky" / "SKILL.md", (clock(), clock()))
    curator.record_use("lint-fix", clock=lambda: clock() - 2 * 86400)
    curator.record_use("lint-repair", clock=lambda: clock() - 2 * 86400)
    for _ in range(3):
        curator.record_use("flaky", ok=False, clock=clock)
    store = ProposalStore(tmp_path)
    res = curator.curate(store, now=clock(), stale_days=30)
    stale = dict(res["stale"])
    assert "deploy" in stale and "unused for 40 days" in stale["deploy"]
    assert "flaky" in stale and "failed" in stale["flaky"]
    assert "lint-fix" not in stale
    merges = [p for p in res["proposals"] if p.payload["op"] == "merge"]
    assert len(merges) == 1 and {merges[0].payload["keep"], merges[0].payload["drop"]} == {"lint-fix", "lint-repair"}
    marks = json.loads((home / "skills" / ".curator.json").read_text())
    assert marks["deploy"]["state"] == "stale"
    assert curator.curate(store, now=clock())["proposals"] == []  # latched
    curator.apply(merges[0].payload)
    assert (home / "skills" / "_archive" / merges[0].payload["drop"]).is_dir()
    assert (home / "skills" / "deploy").is_dir()  # stale skills are only marked, never deleted


def test_facts_section_roundtrip(tmp_path):
    md = tmp_path / "K3CODE.md"
    md.write_text("# A\ntext\n")
    distiller.write_auto_section(md, [distiller.Preference("f1", 1, 1)], review.FACTS_HEADING)
    distiller.write_auto_section(md, [distiller.Preference("f2", 1, 1)], review.FACTS_HEADING)
    assert md.read_text().startswith("# A\ntext\n") and distiller.read_auto_section(md, review.FACTS_HEADING) == ["f2"]
