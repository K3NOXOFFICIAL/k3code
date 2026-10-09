"""E6: the reason given with a denial is kept (scrubbed) and repeated similar reasons become a preference proposal."""

from __future__ import annotations

from k3code.autonomy.proposals import ProposalStore
from k3code.learning import distiller
from k3code.learning.decisions import DecisionLog
from test_learning_gateway import make_server
from test_permissions_gateway import bash, call, run_turn

FAKE_KEY = "sk-" + "q" * 24  # built at run time: the secret scan must not see a literal


async def test_denial_reason_is_recorded_scrubbed(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, [bash("npm install left-pad"), "ok"], monkeypatch)
    await call(server, "session.create", {"cwd": str(tmp_path)})
    await run_turn(server, "go", [{"choice": "deny", "reason": f"use pnpm, not npm (key {FAKE_KEY})"}])
    await server.learning.drain()
    (row,) = DecisionLog(tmp_path.parent / f"{tmp_path.name}-k3home").query("approval")
    assert row["choice"] == "deny"
    assert row["detail"]["reason"].startswith("use pnpm, not npm")
    assert FAKE_KEY not in row["detail"]["reason"] and "redacted" in row["detail"]["reason"]
    assert row["detail"]["command"] == "npm install left-pad"


def _deny(log: DecisionLog, command: str, reason: str, pattern: str = "") -> None:
    log.record(
        "approval",
        cwd="/p/one",
        subject=pattern,
        choice="deny",
        detail={"tool": "bash", "reason": reason, "command": command},
        project="path:/p/one",
    )


def test_two_similar_denials_of_one_prefix_become_a_preference_proposal(tmp_path):
    log, store = DecisionLog(tmp_path), ProposalStore(tmp_path)
    _deny(log, "npm install x", "use pnpm, not npm")
    assert distiller.propose_denial_preferences(log, store) == []  # one reason is not a pattern
    _deny(log, "CI=1 npm run build", "Use pnpm not npm!")
    _deny(log, "npm test", "the tests are slow, ask first")  # same prefix, different reason
    _deny(log, "yarn add y", "use pnpm, not npm")  # similar reason, other prefix
    (p,) = distiller.propose_denial_preferences(log, store)
    assert p.kind == "preference" and "`npm`" in p.text and "use pnpm, not npm" in p.text
    assert p.payload == {"text": "for `npm` commands: use pnpm, not npm"}
    assert distiller.propose_denial_preferences(log, store) == []  # proposed once


def test_denials_without_reason_or_allowed_answers_are_ignored(tmp_path):
    log, store = DecisionLog(tmp_path), ProposalStore(tmp_path)
    for _ in range(3):
        _deny(log, "npm i", "")
        log.record("approval", cwd="/p", subject="npm *", choice="once", detail={"tool": "bash", "reason": "fine"})
    assert distiller.propose_denial_preferences(log, store) == []


async def test_distill_makes_the_proposal_and_accepting_it_writes_user_md(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    sess = await call(server, "session.create", {"cwd": str(tmp_path)})
    for cmd in ("npm install a", "npm ci"):
        _deny(server.learning.log, cmd, "use pnpm, not npm")
    user_md = tmp_path / "USER.md"
    user_md.write_text("# me\n\n## Learned preferences (auto)\n- old\n", encoding="utf-8")
    await distiller.distill(
        server.learning.log, home=server.learning.home, user_md=user_md, proposals=server.learning.store
    )
    (p,) = [p for p in server.learning.store.all() if p.kind == "preference"]
    monkeypatch.setattr("k3code.memory.user_memory_path", lambda: user_md)
    res = await call(
        server, "command.dispatch", {"name": "proposals", "arg": f"accept {p.id}", "session_id": sess["session_id"]}
    )
    assert "remembered" in res["output"]
    text = user_md.read_text(encoding="utf-8")
    # above the auto section: the next distill run rewrites that section and must not drop the line
    assert text.index("- for `npm` commands: use pnpm, not npm") < text.index("## Learned preferences (auto)")
    distiller.write_auto_section(user_md, [])
    assert "- for `npm` commands: use pnpm, not npm" in user_md.read_text(encoding="utf-8")
