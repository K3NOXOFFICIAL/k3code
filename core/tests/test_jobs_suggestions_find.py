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
