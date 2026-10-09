"""Jobs and suggestions lookup order: exact id, exact name/title, unique id prefix (an ambiguous ref finds nothing)."""

from __future__ import annotations

from auto_helpers import FakeRunner, clock, make_db
from k3code.automation.scheduler import JobScheduler
from k3code.automation.suggestions import Suggestions


def _sched(tmp_path):
    db = make_db(tmp_path)
    return db, JobScheduler(db, FakeRunner(), clock())


def _job(db, jid, name):
    db.insert("jobs", id=jid, name=name, prompt="x", schedule={"type": "cron", "expr": "0 3 * * *"}, created_at=1.0)


def _sug(db, sid, title):
    db.insert("suggestions", id=sid, dedup_key="k-" + sid, title=title, created_at=1.0)


def test_job_name_that_looks_like_an_id_prefix_resolves_to_the_named_job(tmp_path):
    db, s = _sched(tmp_path)
    _job(db, "cafe1234", "other")
    _job(db, "11112222", "cafe")
    assert s.find("cafe")["id"] == "11112222"
    assert s.find("11112222")["id"] == "11112222"


def test_pause_and_remove_by_such_a_name_touch_only_the_named_job(tmp_path):
    db, s = _sched(tmp_path)
    _job(db, "cafe1234", "other")
    _job(db, "11112222", "cafe")
    assert s.pause("cafe")
    assert db.get("jobs", "11112222")["state"] == "paused"
    assert db.get("jobs", "cafe1234")["state"] == "active"
    assert s.remove("cafe")
    assert db.get("jobs", "11112222") is None
    assert db.get("jobs", "cafe1234") is not None


def test_job_exact_id_wins_and_unique_prefix_still_works(tmp_path):
    db, s = _sched(tmp_path)
    _job(db, "cafe1234", "x")
    _job(db, "aaaa0000", "cafe1234")
    _job(db, "beef5678", "y")
    assert s.find("cafe1234")["id"] == "cafe1234"
    assert s.find("bee")["id"] == "beef5678"


def test_job_duplicate_name_and_ambiguous_prefix_find_nothing(tmp_path):
    db, s = _sched(tmp_path)
    _job(db, "cafe1234", "dup")
    _job(db, "cafe5678", "dup")
    assert s.find("dup") is None
    assert s.find("caf") is None
    assert s.find("missing") is None
    assert not s.pause("dup") and not s.remove("caf")
    assert {r["state"] for r in db.rows("jobs")} == {"active"} and len(db.rows("jobs")) == 2


def test_suggestion_title_that_looks_like_an_id_prefix_resolves_to_that_suggestion(tmp_path):
    db = make_db(tmp_path)
    g = Suggestions(db, catalog=[])
    _sug(db, "cafe1234", "other")
    _sug(db, "11112222", "cafe")
    assert g.get("cafe")["id"] == "11112222"
    assert g.get("Cafe")["id"] == "11112222"
    assert g.dismiss("cafe")
    assert db.get("suggestions", "11112222")["status"] == "dismissed"
    assert db.get("suggestions", "cafe1234")["status"] == "pending"


def test_suggestion_exact_id_wins_and_unique_prefix_still_works(tmp_path):
    db = make_db(tmp_path)
    g = Suggestions(db, catalog=[])
    _sug(db, "cafe1234", "x")
    _sug(db, "aaaa0000", "cafe1234")
    _sug(db, "beef5678", "y")
    assert g.get("cafe1234")["id"] == "cafe1234"
    assert g.get("bee")["id"] == "beef5678"


def test_suggestion_duplicate_title_and_ambiguous_prefix_find_nothing(tmp_path):
    db = make_db(tmp_path)
    g = Suggestions(db, catalog=[])
    _sug(db, "cafe1234", "dup")
    _sug(db, "cafe5678", "dup")
    assert g.get("dup") is None
    assert g.get("caf") is None
    assert g.get("missing") is None
    assert not g.dismiss("dup") and g.mark_accepted("caf") is None
    assert [r["status"] for r in db.rows("suggestions")] == ["pending", "pending"]


def test_job_name_matches_ignoring_case_and_prefix_wildcards_are_literal(tmp_path):
    db, s = _sched(tmp_path)
    _job(db, "cafe1234", "other")
    _job(db, "11112222", "cafe")
    _job(db, "ab0c1234", "p")
    assert s.find("CAFE")["id"] == "11112222"
    assert s.find("ab_c") is None
    assert s.find("a%") is None
    assert s.find("ab0")["id"] == "ab0c1234"


def test_job_names_differing_only_by_case_find_nothing_for_a_third_spelling(tmp_path):
    db, s = _sched(tmp_path)
    _job(db, "aaaa0001", "Cafe")
    _job(db, "aaaa0002", "cAFE")
    assert s.find("CAFE") is None
    assert s.find("Cafe")["id"] == "aaaa0001"


def test_suggestion_id_prefix_wildcards_are_literal(tmp_path):
    db = make_db(tmp_path)
    g = Suggestions(db, catalog=[])
    _sug(db, "ab0c1234", "one")
    assert g.get("ab_c") is None
    assert g.get("a%") is None
    assert g.get("ab0")["id"] == "ab0c1234"


def test_a_pending_suggestion_wins_over_a_resolved_one_with_the_same_title(tmp_path):
    db = make_db(tmp_path)
    g = Suggestions(db, catalog=[])
    _sug(db, "aaaa0001", "same")
    _sug(db, "bbbb0002", "same")
    db.update("suggestions", "aaaa0001", status="dismissed")
    assert g.get("same")["id"] == "bbbb0002"
    assert g.dismiss("same")
    assert db.get("suggestions", "bbbb0002")["status"] == "dismissed"
    # both resolved now: the lookup falls back to all matches, which are two, so it finds nothing
    assert g.get("same") is None


def test_two_pending_suggestions_with_one_title_stay_ambiguous_even_with_a_resolved_twin(tmp_path):
    db = make_db(tmp_path)
    g = Suggestions(db, catalog=[])
    _sug(db, "aaaa0001", "same")
    _sug(db, "bbbb0002", "same")
    _sug(db, "cccc0003", "same")
    db.update("suggestions", "aaaa0001", status="accepted")
    assert g.get("same") is None
    assert not g.dismiss("same")


def test_a_lone_resolved_suggestion_is_still_found_by_title(tmp_path):
    db = make_db(tmp_path)
    g = Suggestions(db, catalog=[])
    _sug(db, "aaaa0001", "gone")
    db.update("suggestions", "aaaa0001", status="dismissed")
    assert g.get("gone")["id"] == "aaaa0001"
    assert not g.dismiss("gone")
