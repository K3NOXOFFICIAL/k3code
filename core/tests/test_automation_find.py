"""Automation lookup order: exact id, exact name, unique id prefix (an ambiguous ref finds nothing)."""

from __future__ import annotations

from auto_helpers import FakeRunner, clock, make_db
from k3code.automation.automations import AutomationManager


def _mgr(tmp_path):
    db = make_db(tmp_path)
    return db, AutomationManager(db, FakeRunner(), clock())


def _row(db, aid, name, state="active"):
    db.insert(
        "automations",
        id=aid,
        name=name,
        trigger={"type": "cron", "schedule": "0 3 * * *"},
        action={"type": "prompt", "prompt": "x"},
        policy={},
        state=state,
        created_at=1.0,
    )


def test_a_name_that_looks_like_an_id_prefix_resolves_to_the_named_automation(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "cafe1234", "other")
    _row(db, "11112222", "cafe")
    assert m.find("cafe")["id"] == "11112222"
    assert m.find("11112222")["id"] == "11112222"


async def test_pause_and_remove_by_such_a_name_touch_only_the_named_automation(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "cafe1234", "other")
    _row(db, "11112222", "cafe")
    assert await m.pause("cafe")
    assert db.get("automations", "11112222")["state"] == "paused"
    assert db.get("automations", "cafe1234")["state"] == "active"
    assert await m.remove("cafe")
    assert db.get("automations", "11112222") is None
    assert db.get("automations", "cafe1234") is not None


def test_exact_id_wins_over_a_name(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "cafe1234", "x")
    _row(db, "aaaa0000", "cafe1234")
    assert m.find("cafe1234")["id"] == "cafe1234"


def test_unique_id_prefix_still_works(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "cafe1234", "one")
    _row(db, "beef5678", "two")
    assert m.find("caf")["id"] == "cafe1234"
    assert m.find("bee")["id"] == "beef5678"


def test_ambiguous_prefix_and_duplicate_names_find_nothing(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "cafe1234", "dup")
    _row(db, "cafe5678", "dup")
    assert m.find("caf") is None
    assert m.find("dup") is None
    assert m.find("missing") is None


async def test_ambiguous_ref_changes_nothing(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "cafe1234", "dup")
    _row(db, "cafe5678", "dup")
    assert not await m.pause("dup")
    assert not await m.remove("caf")
    assert {r["state"] for r in db.rows("automations")} == {"active"} and len(db.rows("automations")) == 2
