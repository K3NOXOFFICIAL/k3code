from k3code.autonomy.proposals import ProposalStore, propose
from k3code.learning import ranking
from k3code.learning.decisions import DecisionLog
from learn_helpers import FakeCaller, FakeClock

ITEMS = [
    {"kind": "improvement", "text": "refactor the parser module", "action": "a"},
    {"kind": "also_setup", "text": "set up pre-commit hooks", "action": "b"},
]


def decide(log, kind, choice, n, project="p"):
    for _ in range(n):
        log.record("proposal", subject="x", choice=choice, detail={"kind": kind}, project=project)


def test_acceptance_history_changes_ranking(tmp_path):
    clock = FakeClock()
    log = DecisionLog(tmp_path, clock)
    store = ProposalStore(tmp_path)
    base = ranking.rank(ITEMS, log=log, store=store, project="p", now=clock())
    assert base[0]["score"] == base[1]["score"]  # no history: a tie
    decide(log, "also_setup", "accept", 5)
    decide(log, "improvement", "dismiss", 5)
    ranked = ranking.rank(ITEMS, log=log, store=store, project="p", threshold=0, now=clock())
    assert ranked[0]["kind"] == "also_setup"
    assert ranked[0]["score"] > ranked[1]["score"]
    # with the default threshold the always-dismissed kind is dropped entirely
    assert [i["kind"] for i in ranking.rank(ITEMS, log=log, store=store, project="p", now=clock())] == ["also_setup"]
    # another project's history does not leak in when this project has its own
    other = ranking.rank(ITEMS, log=log, store=store, project="q", now=clock())
    assert other[0]["kind"] == "also_setup"  # falls back to the global rate


def test_similarity_to_dismissed_drops_proposal(tmp_path):
    clock = FakeClock()
    log = DecisionLog(tmp_path, clock)
    store = ProposalStore(tmp_path)
    p = store.add("improvement", "refactor the parser module now", "a")
    store.set_status(p.id, "dismissed")
    out = ranking.rank(ITEMS, log=log, store=store, threshold=0.2, now=clock())
    assert [i["kind"] for i in out] == ["also_setup"]


def test_recency_decays(tmp_path):
    clock = FakeClock()
    log = DecisionLog(tmp_path, clock)
    decide(log, "improvement", "accept", 3)
    fresh = ranking.score("zzz new idea", "improvement", log=log, store=None, now=clock())
    clock.advance(90 * 86400)
    stale = ranking.score("zzz new idea", "improvement", log=log, store=None, now=clock())
    assert fresh > stale


async def test_propose_uses_ranker_threshold_and_preferences(tmp_path):
    clock = FakeClock()
    log = DecisionLog(tmp_path, clock)
    decide(log, "improvement", "dismiss", 12)
    store = ProposalStore(tmp_path)
    caller = FakeCaller(
        '[{"kind":"improvement","text":"tidy imports","action":"x"},'
        '{"kind":"also_setup","text":"add a Makefile","action":"y"}]'
    )
    ranker = lambda items: ranking.rank(items, log=log, store=store, threshold=0.15, now=clock())  # noqa: E731
    out = await propose(caller, store, "ctx", ranker=ranker, preferences=["prefers small PRs"])
    assert [p.kind for p in out] == ["also_setup"]
    assert "prefers small PRs" in caller.calls[0][0].content
