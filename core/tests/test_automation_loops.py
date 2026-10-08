"""P1-5: a loop whose session needs input ends as ``blocked`` with one notification, never as a silent stop."""

from __future__ import annotations

from auto_helpers import FakeRunner
from k3code.automation.runner import RunResult
from test_auto_loops import mk, settle


async def test_loop_needs_input_becomes_blocked_and_notifies_once(tmp_path):
    runner = FakeRunner(results=[RunResult(status="needs_input", text="", error="budget exceeded")])
    c, db, r, m = mk(tmp_path, runner=runner)
    row = m.create(session_id="s1", prompt="check build", interval="5m")
    await settle()
    loop = db.get("loops", row["id"])
    assert loop["state"] == "blocked" and "needs input" in loop["stop_reason"]
    assert m.active_count() == 0
    assert len(r.notes) == 1 and "blocked" in r.notes[0][0]
    await c.advance(3600)  # a blocked loop never ticks again
    assert len(r.prompts) == 1
    assert len(r.notes) == 1
    await m.close()


async def test_run_status_blocked_is_not_completed(tmp_path):
    from k3code.automation.server_runner import _STATUS

    assert _STATUS["needs_input"] == "blocked"
