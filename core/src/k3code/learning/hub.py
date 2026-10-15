"""LearningHub: the gateway-facing facade tying the decision log, miners, distiller, prep, review and optimizer."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k3code.autonomy.proposals import Proposal, ProposalStore, dedup_key
from k3code.config import Settings, load_config
from k3code.learning import (
    curator,
    distiller,
    gotchas,
    learning_cfg,
    optimizer,
    permrules,
    projectprep,
    ranking,
    recipes,
    replay,
    review,
)
from k3code.learning.decisions import DecisionLog, project_id
from k3code.learning.updateconfig import merge_patch
from k3code.paths import ensure_private_dir, private_file
from k3code.redact import scrub_text

logger = logging.getLogger(__name__)

DAY = 86400.0
WEEK = 7 * DAY


#: Proposal kinds whose handler writes into the prompt's learned tail (``prompting.learned_sections``): the stack
#: facts, a gotcha, a USER.md line, an optimizer overlay.
PROMPT_KINDS = frozenset({"project_setup", "project_gotcha", "preference", "optimizer"})


class LearningHub:
    def __init__(self, server: Any, store: ProposalStore, clock: Callable[[], float] = time.time) -> None:
        self.server = server
        self.home: Path = server._home()
        self.clock = clock
        self.log = DecisionLog(self.home, clock)
        self.store = store
        self.experiments = optimizer.Experiments(self.home, clock, live=server.config)
        self.experiments.on_config_rollback = self._config_rolled_back
        # load() joins each record's real outcome from the decision log (issue #140)
        self.replays = replay.ReplayStore(self.home, outcomes=self.log.turn_outcomes)
        self._state_path = self.home / "learning" / "state.json"
        self._tasks: set[asyncio.Task[Any]] = set()
        #: (session id, tool) -> (tool_error row id, signature, the failed call's args, the model reply it came from):
        #: waits for the retry that works, which is the next call of the tool after that reply
        self._last_failure: dict[tuple[str, str], tuple[int, str, dict[str, Any], object | None]] = {}

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
        ensure_private_dir(self._state_path.parent)
        private_file(self._state_path).write_text(json.dumps(st), encoding="utf-8")

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

    def approval(
        self, session: Any, tool: str, pattern: str, choice: str, *, reason: str = "", command: str = ""
    ) -> None:
        """``reason``: what the user typed with the answer ("use pnpm, not npm"); it used to reach the model for one
        turn only. Kept scrubbed, so the distiller can turn repeated reasons into a preference."""
        detail: dict[str, Any] = {"tool": tool}
        if reason.strip():
            detail["reason"] = scrub_text(reason.strip())[:300]
        if command.strip():
            detail["command"] = scrub_text(command.strip())[:200]
        self.record("approval", session, subject=pattern, choice=choice, detail=detail)
        if tool == "bash" and self.enabled:
            self.spawn(self._mine_later(session))

    async def _mine_later(self, session: Any) -> None:
        self.mine_permissions(session)

    def tool_outcome(
        self, session: Any, call: Any, result: dict[str, Any], failure: Any, *, reply: object | None = None
    ) -> str | None:
        """Record a failed tool call as a ``tool_error``; the next call of that tool in the session that looks like a
        retry and works becomes the row's hint. ``reply`` says which model reply asked for the call (the same object
        for every call of one reply, None when unknown): a call of the same reply is no retry, the model had not seen
        the failure yet. A signature seen REPEATS times in the project within WINDOW proposes a project gotcha.
        Returns a reminder for the model when the failure already has a lesson (the agent loop sends it after the
        step's tool results), else None. Never raises: it runs inside the turn."""
        if not self.enabled or session is None:
            return None
        try:
            return self._tool_outcome(session, call, result, failure, reply)
        except Exception:  # noqa: BLE001 - learning must never break a turn
            logger.warning("tool error learning failed", exc_info=True)
            return None

    def _tool_outcome(
        self, session: Any, call: Any, result: dict[str, Any], failure: Any, reply: object | None
    ) -> str | None:
        cwd = str(session.perms.cwd)
        key = (session.session_id, call.name)
        pending = self._last_failure.get(key)
        if pending is not None and (reply is None or reply is not pending[3]):
            # the first call of the tool after the failing reply ends the wait, retry or not (a call of the failing
            # reply itself is skipped: it was asked for before the model saw the failure)
            del self._last_failure[key]
        else:
            pending = None
        if failure is None:
            if pending is not None:
                row_id, sig, failed_args, _ = pending
                hint = gotchas.followup_hint(call.name, failed_args, call.arguments)
                if hint:
                    self.log.update_detail(row_id, followup=hint)
                    self._maybe_gotcha(session, cwd, call.name, sig)
            return None
        if failure.error_class in gotchas.NOT_PROJECT or (
            failure.error_class.startswith("exit ") and not str(result.get("stderr") or "").strip()
        ):
            return None  # a failing test run or `grep` without a match says nothing about the project by itself
        row_id = self.log.record(
            "tool_error",
            session=session.session_id,
            cwd=cwd,
            subject=failure.signature,
            choice=failure.error_class,
            detail={"tool": call.name, "error": failure.first_line},
            actor="auto" if getattr(session, "background", False) else "user",
        )
        self._last_failure[key] = (row_id, failure.signature, dict(call.arguments), reply)
        hint = self._maybe_gotcha(session, cwd, call.name, failure.signature)
        return self._known_fix(cwd, call.name, failure.signature, hint)

    def _known_fix(self, cwd: str, tool: str, sig: str, hint: str) -> str | None:
        """The reminder for a failure with a lesson: the project's accepted gotcha line, else its auto-learned one,
        else the user-level one, else the working retry recorded on an earlier row in the window (``hint``). Small
        file reads, no model call."""
        pid = self.log.project_for(cwd)  # cached per cwd
        if self._gotcha_dismissed(pid, tool, sig):
            return None
        for path in (gotchas.gotchas_path(pid, self.home), gotchas.auto_gotchas_path(pid, self.home)):
            if (known := gotchas.lesson(path, tool, sig)) is not None:
                return gotchas.reminder(tool, sig, known)
        if (known := gotchas.lesson(gotchas.user_gotchas_path(self.home), tool, sig)) is not None:
            return gotchas.reminder(tool, sig, known, machine=True)
        return gotchas.reminder(tool, sig, hint) if hint else None

    def _maybe_gotcha(self, session: Any, cwd: str, tool: str, sig: str) -> str:
        """Learn or propose a gotcha for a recurring signature; returns the working retry seen for it in the window
        (its hint), or ""."""
        pid = self.log.project_for(cwd)
        if self._gotcha_dismissed(pid, tool, sig):
            return ""
        seen = self.log.tool_error_sightings(
            pid, tool, sig, self.clock() - gotchas.WINDOW, max(gotchas.REPEATS, gotchas.AUTO_REPEATS)
        )
        hint = seen.followup
        line = f"{tool}: {sig}" + (f" — {hint}" if hint else "")
        if self.cfg["auto_gotchas"] and seen.count >= gotchas.AUTO_REPEATS and gotchas.auto_ok(seen.choice, hint):
            self._auto_gotcha(session, pid, tool, sig, hint, line)
            return hint
        if seen.count >= gotchas.REPEATS:
            self._propose_gotcha(session, pid, tool, sig, hint, line)
        return hint

    def _propose_gotcha(self, session: Any, pid: str, tool: str, sig: str, hint: str, line: str) -> None:
        """The card that puts ``line`` into the project's system prompt when accepted; once per signature."""
        p = self.store.add(
            "project_gotcha",
            gotchas.proposal_text(sig, hint),
            "remember this pitfall for the project",
            session.session_id,
            payload={"project": pid, "line": line},
            project=pid,
            key=self._gotcha_key(pid, tool, sig),
        )
        if p is not None:
            self.emit(session, [p])

    @staticmethod
    def _gotcha_key(pid: str, tool: str, sig: str) -> str:
        return dedup_key("project_gotcha", f"{pid} {tool} {sig}")

    def _gotcha_dismissed(self, pid: str, tool: str, sig: str) -> bool:
        """Whether the card for this signature was dismissed: it is then neither reminded nor learned again."""
        return self.store.dismissed(self._gotcha_key(pid, tool, sig))

    def _auto_gotcha(self, session: Any, pid: str, tool: str, sig: str, hint: str, line: str) -> None:
        """Note a self-verified gotcha (a retry worked) for the in-turn reminders, propose its card and tell the
        session in one line. The line is untrusted text (tool output and the model's retry), so it stays out of the
        system prompt until the card is accepted. A signature the project already has a lesson for is left alone, so a
        recurring failure is noted and announced once."""
        path = gotchas.auto_gotchas_path(pid, self.home)
        if any(gotchas.has_lesson(p, tool, sig) for p in (gotchas.gotchas_path(pid, self.home), path)):
            return
        gotchas.append_gotcha(path, line)
        self._propose_gotcha(session, pid, tool, sig, hint, line)
        text = f"Noted for reminders in this project: {line} (accept its card to add it to the system prompt)"
        session.emit("notification", {"session_id": session.session_id, "text": text})

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
        if choice == "dismiss" and p.kind == "project_gotcha" and p.payload.get("project") and p.payload.get("line"):
            # the card's line was also noted for reminders (``_auto_gotcha``): dismissing it forgets that line too
            auto = gotchas.auto_gotchas_path(str(p.payload["project"]), self.home)
            gotchas.remove_gotcha(auto, str(p.payload["line"]))
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
        msg = await self._apply(p, session)
        if p.kind in PROMPT_KINDS and hasattr(session, "refresh_prompt_inputs"):
            # what the user accepted applies to this conversation now; background writers wait for a compaction
            session.refresh_prompt_inputs()
        return msg

    async def _apply(self, p: Proposal, session: Any = None) -> str:
        payload = p.payload
        cwd = str(session.perms.cwd) if session is not None else ""
        if payload.get("op") == "recipe":  # before the kind switch: a recipe "skill" is not a curator skill
            msg = recipes.apply(payload)
            if session is not None:
                session.perms.reload()
            if payload.get("type") == "mcp":
                await self._reload_mcp(str(payload.get("root") or cwd))
            return msg
        if p.kind == "permission_rule":
            msg = permrules.apply(payload, cwd=cwd)
            if payload.get("auto_do") and payload.get("project"):
                live = self.server.config.autonomy
                live["auto_do_projects"] = [*(live.get("auto_do_projects") or []), payload["project"]]
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
        if p.kind == "project_gotcha" and payload.get("project") and payload.get("line"):
            path = gotchas.gotchas_path(str(payload["project"]), self.home)
            gotchas.append_gotcha(path, str(payload["line"]))
            gotchas.promote_if_shared(self.home, str(payload["line"]))  # accepted in PROMOTE_PROJECTS projects
            return f"added to the known pitfalls of this project ({path})"
        if p.kind == "preference" and payload.get("text"):
            from k3code.memory import user_memory_path

            distiller.add_user_line(user_memory_path(), str(payload["text"]))
            return f"remembered in {user_memory_path()}"
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
        if "prev" in exp:  # the shared config changed (before/after): routers built from it are stale
            self._reset_routers()
        return (
            f"Experiment {exp['id']} started ({optimizer.describe_design(exp)}): active for at least "
            f"{exp['target_sessions']} sessions, then compared (auto-rollback if worse or inconclusive). "
            "/optimizer status"
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
        """Scan the session's project (in a worker thread); new or changed stacks get their proposals."""
        if not self.enabled or session.background:
            return []
        made = await projectprep.prepare(
            Path(session.stored.cwd or "."),
            store=self.store,
            caller=self.server.model_caller,
            preferences=self.preferences(),
            session_id=session.session_id,
            clock=self.clock,
            skill_roots=list(self.server.config.skills.roots),
        )
        self.emit(session, made)
        return made

    async def _reload_mcp(self, cwd: str) -> None:
        """An accepted recipe MCP server: restart the set the way ``/mcp reload`` does."""
        from k3code import mcpjson

        mcp = getattr(self.server, "mcp", None)
        if mcp is None or not cwd:
            return
        try:
            await mcp.reload(mcpjson.merged(self.server.config.mcp.servers, cwd))
        except Exception:  # noqa: BLE001 - the server is saved; /mcp reload retries
            logger.warning("MCP reload after a recipe failed", exc_info=True)

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
            # the sizes the session's prompt had: its own settings (a treatment arm's limits), not the shared ones
            ctx = optimizer.session_settings(self.server.config, session.session_id).context or {}
            tok_in, tok_out = self.server.usage.turn_tokens(turn_id) if turn_id else (0, 0)
            skill_text = skills_prompt(
                cwd, list(self.server.config.skills.roots), limit=int(ctx.get("skill_prompt_limit", PROMPT_LIMIT))
            )
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

    # ── turn outcomes (issue #140): what the improvement engine scores a turn by ──

    def record_turn_outcome(
        self,
        session: Any,
        turn_id: str,
        *,
        status: str,
        tier: str,
        kind: str,
        interrupted: bool,
        test: str | None,
        denials: int,
        msg_start: int,
        msg_end: int,
    ) -> None:
        """Keep one finished turn's outcome signals (see ``decisions._TURN_COLUMNS``). It stays pending until the
        session's next turn, its end or ``OUTCOME_SETTLE_S`` shows it was not undone. Never raises: it runs at turn
        end."""
        if not self.enabled or not turn_id:
            return
        try:
            self.log.record_turn(
                turn_id,
                ts=self.clock(),
                session=session.session_id,
                kind=kind,
                tier=tier,
                status=status,
                interrupted=interrupted,
                test=test,
                denials=denials,
                msg_start=msg_start,
                msg_end=msg_end,
                actor="auto" if getattr(session, "background", False) else "user",
            )
        except Exception:  # noqa: BLE001 - learning must never break a turn
            logger.warning("turn outcome write failed", exc_info=True)

    def turn_judged(self, turn_id: str, verdict: str, gate: str | None) -> None:
        """The goal judge's verdict on a turn, and the ``--check`` result when one ran after it."""
        if not self.enabled or not turn_id or verdict == "inactive":
            return
        try:
            self.log.update_turn(turn_id, judge=verdict, gate=gate)
        except Exception:  # noqa: BLE001
            logger.warning("turn verdict write failed", exc_info=True)

    def turn_undone(self, session: Any, index: int) -> str | None:
        """/undo or /retry is about to drop the session's stored messages from ``index`` on: the pending turn those
        messages belong to is marked undone, and the undo is logged. Returns the turn id, or None."""
        if not self.enabled:
            return None
        try:
            turn = self.log.mark_undone(session.session_id, index)
        except Exception:  # noqa: BLE001
            logger.warning("turn undo write failed", exc_info=True)
            turn = None
        self.record("undo", session, subject=turn or "", choice="turn-undo")
        return turn

    def settle_turns(self, session_id: str) -> None:
        """The session moved on (its next prompt began, or it closed): its pending outcomes are final."""
        if not self.enabled or not session_id:
            return
        try:
            self.log.settle_turns(session_id)
        except Exception:  # noqa: BLE001
            logger.warning("turn settle failed", exc_info=True)

    def discard_turn(self, turn_id: str) -> None:
        """An attempt that is thrown away and run again (context overflow): its outcome is not the turn's, so it is
        kept as ``discarded`` (never scored). Deleting it would leave its replay record to read as a failed turn."""
        if not self.enabled or not turn_id:
            return
        try:
            self.log.update_turn(turn_id, status="discarded", settled=self.clock())
        except Exception:  # noqa: BLE001
            logger.warning("turn outcome discard failed", exc_info=True)

    async def turn_finished(self, session: Any, status: str) -> None:
        if not self.enabled:
            return
        self.mine_permissions(session)
        msgs = session.stored.messages
        meta = session.stored.meta
        turns = review.session_turns(meta, msgs)
        reviewed = int(meta.get("reviewed_turns", 0))
        if reviewed > turns:  # a mark from before the total was kept: it counted the compacted list, which is shorter
            reviewed = 0
        minimum = int(self.cfg["review_min_turns"])
        if status == "done" and turns > minimum and turns > reviewed + minimum - 1:
            meta["reviewed_turns"] = turns
            project = project_id(session.stored.cwd or ".")
            r = await review.review_session(
                self.server.model_caller,
                msgs,
                turns=turns,
                store=self.store,
                cwd=Path(session.stored.cwd or "."),
                config=self.server.config,
                min_turns=minimum,
                session_id=session.session_id,
                project=project,
                ranker=self.ranker(project),
                max_skill_cards=int(self.cfg["skill_cards_per_week"]),
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
        """Analyse the week and propose changes. Only while ``learning.optimizer.enabled`` is on are gate-approved
        candidates applied by themselves; while it is off (the default) they become proposals for a human."""
        m = self.metrics(since=self.clock() - WEEK)
        if m["sessions"] < int(self.cfg["optimizer"]["min_sessions"]):
            return []
        made = optimizer.propose(m, self.server.config, self.store)
        # Token-reducing candidates, replayed over the recorded turns. Only those that pass the auto-apply gate are
        # applied, as an experiment that rolls back through the live config when it turns out worse; the rest wait
        # for a human to accept them.
        context = dict(self.server.config.context or {})
        cands = await replay.token_candidates(context, self.replays.load())
        # a candidate the A/B window already judged worse and rolled back is never auto-applied again: it goes to
        # the proposal store, whose dedup key latches it for the human path
        for c in cands:
            if c["auto_ok"] and (not self.cfg["optimizer"]["enabled"] or self._rolled_back_before(c["patch"])):
                c["auto_ok"] = False
        for c in cands:
            if c["auto_ok"] and not self._already_in_effect(c["patch"]):
                self._auto_apply(c)
        made += optimizer.propose_replayed([c for c in cands if not c["auto_ok"]], self.store)
        if session is not None:
            self.emit(session, made)
        return made

    def _rolled_back_before(self, patch: dict[str, Any]) -> bool:
        """Judged worse, or not shown to be no worse (inconclusive): either way not auto-applied again."""
        return any(
            x["patch"] == patch and x["status"] in ("rolled_back", "inconclusive")
            for x in self.experiments.all()
            if x.get("kind") == "config"
        )

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
            f"Optimizer applied automatically (experiment {exp['id']}, {optimizer.describe_design(exp)}): "
            f"{cand['title']}. Replay: tokens "
            f"-{cand['evidence']['token_reduction_pct']}%, pass drop at most "
            f"{cand['evidence']['pass_drop_points']} points. Rolled back automatically if worse."
        )
