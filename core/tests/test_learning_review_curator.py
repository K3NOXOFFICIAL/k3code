import json

from k3code.autonomy.proposals import ProposalStore
from k3code.learning import curator, distiller, review
from k3code.skills import discover
from learn_helpers import FakeCaller, FakeClock

REPLY = json.dumps(
    {
        "facts": ["tests run with `uv run pytest -q`", "api_key=hunter2 is used"],
        "skills": [
            {
                "name": "release-notes",
                "description": "write release notes",
                "body": "1. list commits\n2. group by type",
            },
            {"name": "Bad Name", "description": "x", "body": "y"},
        ],
    }
)


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
    r = await review.review_session(
        FakeCaller(REPLY),
        convo(8),
        store=store,
        cwd=tmp_path / "repo",
        config=cfg,
        min_turns=6,
        mem0_post=lambda u, b, h: sent.append(b),
    )
    draft = tmp_path / "home" / "skills" / "_drafts" / "release-notes" / "SKILL.md"
    assert draft.is_file() and "group by type" in draft.read_text()
    assert not (tmp_path / "home" / "skills" / "Bad Name").exists()
    # facts go to $K3CODE_HOME/projects/<key>/learned.md, never into the repository's memory file
    assert (tmp_path / "repo" / "K3CODE.md").read_text() == "# Mine\nhand written\n"
    from k3code.memory import learned_notes_path

    mem = learned_notes_path(tmp_path / "repo").read_text()
    assert "uv run pytest -q" in mem and "hunter2" not in mem
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


async def test_review_mem0_writes_do_not_block_the_event_loop_thread(tmp_path):
    import threading

    from k3code.config import Mem0Config, Settings

    (tmp_path / "repo").mkdir()
    threads: list[threading.Thread] = []
    await review.review_session(
        FakeCaller(REPLY),
        convo(8),
        store=ProposalStore(tmp_path),
        cwd=tmp_path / "repo",
        config=Settings(mem0=Mem0Config(url="http://m")),
        min_turns=6,
        mem0_post=lambda u, b, h: threads.append(threading.current_thread()),
    )
    assert threads and all(t is not threading.main_thread() for t in threads)


def _git_repo(path, remote):
    import subprocess

    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)


async def _review_into_mem0(tmp_path, repo, *, project=""):
    from k3code.config import Mem0Config, Settings

    sent: list[dict] = []
    await review.review_session(
        FakeCaller(REPLY),
        convo(8),
        store=ProposalStore(tmp_path),
        cwd=repo,
        config=Settings(mem0=Mem0Config(url="http://m")),
        min_turns=6,
        mem0_post=lambda u, b, h: sent.append(b),
        project=project,
    )
    return sent


async def test_review_facts_posted_to_mem0_carry_the_project_scope(tmp_path, monkeypatch):
    """A project fact ('tests run with uv run pytest') posted bare is recalled in every other repo as a user trait."""
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "billing"
    _git_repo(repo, "https://x-access-token:ghp_notarealtoken@github.com/secretco/secretco-billing.git")
    (body,) = await _review_into_mem0(tmp_path, repo)
    assert body["metadata"]["scope"] == "project"
    assert body["metadata"]["project"] == "git:github.com/secretco/secretco-billing"
    assert body["metadata"]["repo"] == "secretco-billing"
    text = body["messages"][0]["content"]
    assert "secretco-billing" in text and "uv run pytest -q" in text
    assert "ghp_notarealtoken" not in json.dumps(body)  # the remote's userinfo never leaves the machine
    assert body["metadata"]["source"] == "k3code-distiller"  # what a mem0 filter already keys on stays


async def test_review_facts_scope_a_repo_without_a_remote_by_its_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    (tmp_path / "scratch").mkdir()
    (body,) = await _review_into_mem0(tmp_path, tmp_path / "scratch")
    assert body["metadata"]["project"].startswith("path:") and body["metadata"]["repo"] == "scratch"
    assert "scratch" in body["messages"][0]["content"]


async def test_the_same_fact_in_two_projects_is_posted_for_each(tmp_path, monkeypatch):
    """The posted ledger de-duplicates by text; a fact true in one repo must still reach mem0 for the other."""
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    sent = await _review_into_mem0(tmp_path, tmp_path / "a")
    sent += await _review_into_mem0(tmp_path, tmp_path / "b")
    assert [s["metadata"]["repo"] for s in sent] == ["a", "b"]
    again = await _review_into_mem0(tmp_path, tmp_path / "a")
    assert again == []  # but the same fact in the same project is still posted once


def test_a_user_preference_stays_unscoped(tmp_path):
    from k3code.config import Mem0Config, Settings

    sent: list[dict] = []
    cfg = Settings(mem0=Mem0Config(url="http://m"))
    distiller.store_mem0(cfg, [distiller.Preference("prefers tabs", 0.9, 4)], lambda u, b, h: sent.append(b))
    (body,) = sent
    assert body["messages"][0]["content"] == "User preference (learned by k3code): prefers tabs"
    assert set(body["metadata"]) == {"confidence", "evidence", "source"}
