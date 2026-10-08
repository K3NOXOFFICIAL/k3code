"""LearningHub: the gateway-facing facade tying the decision log, miners, distiller, prep, review and optimizer."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import Proposal, ProposalStore
from k3code.learning import curator, distiller, learning_cfg, optimizer, permrules, projectprep, ranking, review
from k3code.learning.decisions import DecisionLog, project_id

logger = logging.getLogger(__name__)

DAY = 86400.0
WEEK = 7 * DAY


class LearningHub:
    def __init__(self, server: Any, store: ProposalStore, clock: Callable[[], float] = time.time) -> None:
        self.server = server
        self.home: Path = server._home()
        self.clock = clock
        self.log = DecisionLog(self.home, clock)
        self.store = store
        self.experiments = optimizer.Experiments(self.home, clock)
        self._state_path = self.home / "learning" / "state.json"
        self._tasks: set[asyncio.Task[Any]] = set()

    # ── config / state ──

    @property
    def cfg(self) -> dict[str, Any]:
        return learning_cfg(self.server.config)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg["enabled"])

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _set_state(self, **kv: Any) -> None:
        st = {**self._state(), **kv}
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(st), encoding="utf-8")

    def spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait until every spawned task has finished.

        Finished tasks are dropped here rather than left to their done-callbacks: gather() returns at once for finished
        tasks, so a finished task whose discard callback had not run yet made the old loop spin without yielding.
        """
        while True:
            self._tasks.difference_update({t for t in self._tasks if t.done()})
            pending = list(self._tasks)
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    # ── recording ──

    def record(self, kind: str, session: Any = None, **kw: Any) -> None:
        if not self.enabled:
            return
        try:
            cwd = kw.pop("cwd", None) or (str(session.perms.cwd) if session is not None else "")
            sid = session.session_id if session is not None else kw.pop("session_id", "")
            # unattended runs (bg, loop ticks, cron, sub-agents of those) are pre-approved, not user decisions
            kw.setdefault("actor", "auto" if session is not None and getattr(session, "background", False)
                          else "user")
            self.log.record(kind, session=sid, cwd=cwd, **kw)
        except Exception:  # noqa: BLE001 - learning must never break a turn
            logger.warning("decision log write failed", exc_info=True)

    def approval(self, session: Any, tool: str, pattern: str, choice: str) -> None:
        self.record("approval", session, subject=pattern, choice=choice, detail={"tool": tool})
        if tool == "bash" and self.enabled:
            self.spawn(self._mine_later(session))

    async def _mine_later(self, session: Any) -> None:
        self.mine_permissions(session)

    # ── proposals ──

    def preferences(self, top: int = 5) -> list[str]:
        return [p.text for p in distiller.load_preferences(self.home, top)]

    def ranker(self, project: str) -> Callable[[list[dict[str, str]]], list[dict[str, str]]]:
        return lambda items: ranking.rank(items, log=self.log, store=self.store, project=project,
                                          threshold=float(self.cfg["rank_threshold"]), now=self.clock())

    def emit(self, session: Any, proposals: list[Proposal]) -> None:
        for p in proposals:
            self.server.autonomy.emit_proposal(session, p)

    def mine_permissions(self, session: Any) -> list[Proposal]:
        c = self.cfg
        cands = permrules.mine(self.log, min_approvals=int(c["perm_min_approvals"]),
                               min_denials=int(c["perm_min_denials"]), user_projects=int(c["user_scope_projects"]),
                               cwd=str(session.perms.cwd) if session is not None else "")
        made = permrules.to_proposals(cands, self.store, session.session_id if session else "")
        if session is not None:
            self.emit(session, made)
        return made

    def decide(self, p: Proposal, choice: str, session: Any = None) -> None:
        self.record("proposal", session, subject=p.text[:200], choice=choice,
                    detail={"kind": p.kind, "id": p.id}, project=p.project or None)

    async def apply(self, p: Proposal, session: Any = None) -> str:
        """Run the handler of an accepted learned proposal; returns a human message."""
        payload = p.payload
        cwd = str(session.perms.cwd) if session is not None else ""
        if p.kind == "permission_rule":
            msg = permrules.apply(payload, cwd=cwd)
            if payload.get("auto_do"):
                self.server.config.autonomy["auto_do_plans"] = True
            if session is not None:
                session.perms.reload()
            return msg
        if p.kind == "project_setup":
            msg = await projectprep.apply(payload, self.server.model_caller,
                                          session.session_id if session is not None else "")
            if session is not None:
                session.perms.reload()
            return msg
        if p.kind == "skill":
            return curator.apply(payload)
        if p.kind == "optimizer":
            if "task_tiers" in payload:
                return self._apply_overlay({"title": p.text, "patch": {"task_tiers": payload["task_tiers"]},
                                            "evidence": {}})
            return self._apply_overlay(payload["overlay"])
        return "noted"

    def _apply_overlay(self, overlay: dict[str, Any]) -> str:
        baseline = self.metrics(since=self.clock() - WEEK)
        try:
            exp = self.experiments.start(overlay, baseline, sessions=int(self.cfg["optimizer"]["ab_sessions"]))
        except Exception as e:  # noqa: BLE001
            return f"overlay not applied: {e}"
        if "patch" in overlay:
            for k, v in overlay["patch"].items():
                cur = getattr(self.server.config, k, None)
                if isinstance(cur, dict) and isinstance(v, dict):
                    cur.update(v)
        return (f"Experiment {exp['id']} started: active for {exp['target_sessions']} sessions, then compared "
                f"(auto-rollback if worse). /optimizer status")

    # ── project prep ──

    async def prepare_project(self, session: Any) -> list[Proposal]:
        root = Path(session.stored.cwd or ".")
        if not self.enabled or not projectprep.needs_prep(root) or session.background:
            return []
        made = await projectprep.prepare(root, store=self.store, caller=self.server.model_caller,
                                         preferences=self.preferences(), session_id=session.session_id,
                                         clock=self.clock)
        self.emit(session, made)
        return made

    # ── turn end ──

    def metrics(self, since: float = 0.0, until: float | None = None) -> dict[str, Any]:
        return optimizer.collect(self.server.usage.rows(since), self.log, self.server.autonomy.scope_log.read(),
                                 since=since, until=until)

    async def turn_finished(self, session: Any, status: str) -> None:
        if not self.enabled:
            return
        self.mine_permissions(session)
        msgs = session.stored.messages
        turns = review.user_turns(msgs)
        meta = session.stored.meta
        minimum = int(self.cfg["review_min_turns"])
        if status == "done" and turns > minimum and turns > int(meta.get("reviewed_turns", 0)) + minimum - 1:
            meta["reviewed_turns"] = turns
            r = await review.review_session(
                self.server.model_caller, msgs, store=self.store, cwd=Path(session.stored.cwd or "."),
                config=self.server.config, min_turns=minimum, session_id=session.session_id,
                project=project_id(session.stored.cwd or "."))
            self.emit(session, r["proposals"])
        if self.experiments.active():
            self.experiments.session_done(lambda since: self.metrics(since=since), notify=self._notify)
        await self.maintenance(session)

    def _notify(self, text: str) -> None:
        emit = getattr(self.server, "emit", None)
        if emit is not None:
            emit("notification", {"text": text})

    async def maintenance(self, session: Any = None, *, force: bool = False) -> dict[str, Any]:
        """Daily distill + curate, weekly optimizer (opt-in). Cheap tier; safe to call often."""
        now = self.clock()
        st = self._state()
        done: dict[str, Any] = {}
        if force or now - st.get("distilled", 0) > DAY:
            from k3code.memory import user_memory_path

            prefs = await distiller.distill(self.log, home=self.home, user_md=user_memory_path(),
                                            config=self.server.config, caller=self.server.model_caller,
                                            proposals=self.store, session_id=session.session_id if session else "")
            done["preferences"] = len(prefs)
            r = curator.curate(self.store, now=now, stale_days=int(self.cfg["stale_days"]))
            done["curated"] = len(r["proposals"])
            if session is not None:
                self.emit(session, r["proposals"])
            self._set_state(distilled=now)
        if (force or now - st.get("optimized", 0) > WEEK) and self.cfg["optimizer"]["enabled"]:
            done["optimizer"] = len(self.run_optimizer(session))
            self._set_state(optimized=now)
        return done

    def run_optimizer(self, session: Any = None) -> list[Proposal]:
        m = self.metrics(since=self.clock() - WEEK)
        if m["sessions"] < int(self.cfg["optimizer"]["min_sessions"]):
            return []
        made = optimizer.propose(m, self.server.config, self.store)
        if session is not None:
            self.emit(session, made)
        return made
