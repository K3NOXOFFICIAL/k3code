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
from k3code.config import Settings, load_config
from k3code.learning import curator, distiller, learning_cfg, optimizer, permrules, projectprep, ranking, replay, review
from k3code.learning.decisions import DecisionLog, project_id
from k3code.learning.updateconfig import merge_patch

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
        self.experiments = optimizer.Experiments(self.home, clock, live=server.config)
        self.experiments.on_config_rollback = self._config_rolled_back
        self.replays = replay.ReplayStore(self.home)
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
            kw.setdefault("actor", "auto" if session is not None and getattr(session, "background", False) else "user")
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
        return lambda items: ranking.rank(
            items,
            log=self.log,
            store=self.store,
            project=project,
            threshold=float(self.cfg["rank_threshold"]),
            now=self.clock(),
        )

    def emit(self, session: Any, proposals: list[Proposal]) -> None:
        for p in proposals:
            self.server.autonomy.emit_proposal(session, p)

    def mine_permissions(self, session: Any) -> list[Proposal]:
        c = self.cfg
        cands = permrules.mine(
            self.log,
            min_approvals=int(c["perm_min_approvals"]),
            min_denials=int(c["perm_min_denials"]),
            user_projects=int(c["user_scope_projects"]),
            cwd=str(session.perms.cwd) if session is not None else "",
        )
        made = permrules.to_proposals(cands, self.store, session.session_id if session else "")
        if session is not None:
            self.emit(session, made)
        return made

    def decide(self, p: Proposal, choice: str, session: Any = None) -> None:
        self.record(
            "proposal",
            session,
            subject=p.text[:200],
            choice=choice,
            detail={"kind": p.kind, "id": p.id},
            project=p.project or None,
        )

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
            msg = await projectprep.apply(
                payload, self.server.model_caller, session.session_id if session is not None else ""
            )
            if session is not None:
                session.perms.reload()
            return msg
        if p.kind == "skill":
            return curator.apply(payload)
        if p.kind == "optimizer":
            if "task_tiers" in payload:
                return self._apply_overlay(
                    {"title": p.text, "patch": {"task_tiers": payload["task_tiers"]}, "evidence": {}}
                )
            return self._apply_overlay(payload["overlay"])
        return "noted"

    def _apply_overlay(self, overlay: dict[str, Any]) -> str:
        baseline = self.metrics(since=self.clock() - WEEK)
        try:
            exp = self.experiments.start(overlay, baseline, sessions=int(self.cfg["optimizer"]["ab_sessions"]))
        except Exception as e:  # noqa: BLE001
            return f"overlay not applied: {e}"
        if "patch" in overlay:
            self._reset_routers()
        return (
            f"Experiment {exp['id']} started: active for {exp['target_sessions']} sessions, then compared "
            f"(auto-rollback if worse). /optimizer status"
        )

    def _config_rolled_back(self, exp: dict[str, Any]) -> None:
        """rollback() restored the config file only: the live config kept the experiment's values until a restart."""
        fresh = load_config(project_dir=Path.cwd())
        for k in exp.get("patch") or {}:
            if k not in Settings.model_fields:
                continue
            cur, new = getattr(self.server.config, k, None), getattr(fresh, k)
            if isinstance(cur, dict) and isinstance(new, dict):
                cur.clear()  # in place, like _apply_overlay: holders of the dict see the restored values
                cur.update(new)
            else:
                setattr(self.server.config, k, new)
        self._reset_routers()

    def _reset_routers(self) -> None:
        """Tier routers capture task tiers and router options when built: rebuild them on next use."""
        reset = getattr(self.server, "reset_tier_routers", None)
        if reset is not None:
            reset()

    # ── project prep ──

    async def prepare_project(self, session: Any) -> list[Proposal]:
        root = Path(session.stored.cwd or ".")
        if not self.enabled or not projectprep.needs_prep(root) or session.background:
            return []
        made = await projectprep.prepare(
            root,
            store=self.store,
            caller=self.server.model_caller,
            preferences=self.preferences(),
            session_id=session.session_id,
            clock=self.clock,
        )
        self.emit(session, made)
        return made

    # ── turn end ──

    def metrics(self, since: float = 0.0, until: float | None = None) -> dict[str, Any]:
        return optimizer.collect(
            self.server.usage.rows(since), self.log, self.server.autonomy.scope_log.read(), since=since, until=until
        )

    def record_turn_replay(
        self,
        session: Any,
        messages: list[Any],
        status: str,
        tier: str,
        kind: str,
        turn_id: str,
        *,
        history_len: int = 0,
    ) -> None:
        """Keep one finished turn's inputs and outcome for the replay harness. Never raises: it runs at turn end."""
        if not self.enabled:
            return
        try:
            from k3code.memory import memory_prompt
            from k3code.skills import PROMPT_LIMIT, skills_prompt

            cwd = session.perms.cwd
            ctx = self.server.config.context or {}
            tok_in, tok_out = self.server.usage.turn_tokens(turn_id) if turn_id else (0, 0)
            skill_text = skills_prompt(cwd, list(self.server.config.skills.roots), limit=PROMPT_LIMIT)
            record = replay.build_record(
                turn=turn_id,
                session=session.session_id,
                kind=kind,
                tier=tier,
                status=status,
                messages=messages,
                first=1 + history_len,
                tokens_in=tok_in,
                tokens_out=tok_out,
                memory_chars=len(memory_prompt(cwd, limit=int(ctx.get("memory_chars", 20_000)))),
                skill_lines=[len(line) for line in skill_text.splitlines() if line.startswith("- ")],
                ts=self.clock(),
            )
            self.replays.append(record)
        except Exception:  # noqa: BLE001 - recording must never break a turn
            logger.warning("replay record failed", exc_info=True)

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
                self.server.model_caller,
                msgs,
                store=self.store,
                cwd=Path(session.stored.cwd or "."),
                config=self.server.config,
                min_turns=minimum,
                session_id=session.session_id,
                project=project_id(session.stored.cwd or "."),
            )
            self.emit(session, r["proposals"])
        # Once per session and experiment (this ran on every turn), and never for background/cron/loop runs.
        counted = set(meta.get("experiments_counted") or [])
        fresh = {x["id"] for x in self.experiments.active()} - counted
        if fresh and not session.background:
            meta["experiments_counted"] = sorted(counted | fresh)
            self.experiments.session_done(lambda since: self.metrics(since=since), notify=self._notify, ids=fresh)
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

            prefs = await distiller.distill(
                self.log,
                home=self.home,
                user_md=user_memory_path(),
                config=self.server.config,
                caller=self.server.model_caller,
                proposals=self.store,
                session_id=session.session_id if session else "",
            )
            done["preferences"] = len(prefs)
            r = curator.curate(self.store, now=now, stale_days=int(self.cfg["stale_days"]))
            done["curated"] = len(r["proposals"])
            if session is not None:
                self.emit(session, r["proposals"])
            self._set_state(distilled=now)
        if (force or now - st.get("optimized", 0) > WEEK) and self.cfg["optimizer"]["enabled"]:
            done["optimizer"] = len(await self.run_optimizer(session))
            self._set_state(optimized=now)
        return done

    async def run_optimizer(self, session: Any = None) -> list[Proposal]:
        m = self.metrics(since=self.clock() - WEEK)
        if m["sessions"] < int(self.cfg["optimizer"]["min_sessions"]):
            return []
        made = optimizer.propose(m, self.server.config, self.store)
        # Token-reducing candidates, replayed over the recorded turns. Only those that pass the auto-apply gate are
        # applied, as an experiment that rolls back through the live config when it turns out worse; the rest wait
        # for a human to accept them.
        context = dict(self.server.config.context or {})
        cands = await replay.token_candidates(context, self.replays.load())
        for c in cands:
            if c["auto_ok"] and not self._already_in_effect(c["patch"]):
                self._auto_apply(c)
        made += optimizer.propose_replayed([c for c in cands if not c["auto_ok"]], self.store)
        if session is not None:
            self.emit(session, made)
        return made

    def _already_in_effect(self, patch: dict[str, Any]) -> bool:
        if any(x["patch"] == patch for x in self.experiments.active() if x.get("kind") == "config"):
            return True
        return merge_patch(dict(self.server.config.context or {}), patch.get("context", {})) == dict(
            self.server.config.context or {}
        )

    def _auto_apply(self, cand: dict[str, Any]) -> None:
        """Apply a gate-approved candidate as an experiment and log it; the A/B window judges and rolls it back."""
        overlay = {"title": cand["title"], "patch": cand["patch"], "evidence": cand["evidence"], "auto": True}
        baseline = self.metrics(since=self.clock() - WEEK)
        try:
            exp = self.experiments.start(overlay, baseline, sessions=int(self.cfg["optimizer"]["ab_sessions"]))
        except Exception as e:  # noqa: BLE001 - a rejected patch is logged as not applied, never half applied
            logger.warning("auto-apply refused for %r: %s", cand["title"], e)
            return
        self.log.record(
            "auto_apply",
            subject=cand["title"],
            choice="applied",
            actor="auto",
            detail={"experiment": exp["id"], "evidence": cand["evidence"]},
        )
        self._notify(
            f"Optimizer applied automatically (experiment {exp['id']}): {cand['title']}. Replay: tokens "
            f"-{cand['evidence']['token_reduction_pct']}%, pass drop at most "
            f"{cand['evidence']['pass_drop_points']} points. Rolled back automatically if worse."
        )
