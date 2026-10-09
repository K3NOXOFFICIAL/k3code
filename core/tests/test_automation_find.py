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


def test_a_name_matches_ignoring_case_before_any_id_prefix(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "cafe1234", "x")
    _row(db, "11112222", "cafe")
    assert m.find("CAFE")["id"] == "11112222"
    assert m.find("Cafe")["id"] == "11112222"


async def test_pause_by_a_differently_cased_name_touches_only_the_named_automation(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "cafe1234", "x")
    _row(db, "11112222", "cafe")
    assert await m.pause("CAFE")
    assert db.get("automations", "11112222")["state"] == "paused"
    assert db.get("automations", "cafe1234")["state"] == "active"


def test_names_differing_only_by_case_are_ambiguous_for_a_third_spelling(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "aaaa0001", "Cafe")
    _row(db, "aaaa0002", "cAFE")
    assert m.find("CAFE") is None
    assert m.find("Cafe")["id"] == "aaaa0001"  # an exact name still wins


def test_prefix_wildcards_are_literal(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "ab0c1234", "one")
    _row(db, "beef0000", "three")
    assert m.find("ab_c") is None
    assert m.find("a%") is None
    assert m.find("%") is None
    assert m.find("_eef") is None
    assert m.find("ab0")["id"] == "ab0c1234"


def test_prefix_is_still_case_insensitive(tmp_path):
    db, m = _mgr(tmp_path)
    _row(db, "cafe1234", "one")
    assert m.find("CAF")["id"] == "cafe1234"
