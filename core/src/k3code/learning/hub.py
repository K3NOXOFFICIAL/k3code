"""LearningHub: the gateway-facing facade tying the decision log, miners, distiller, prep, review and optimizer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
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
from k3code.learning.fsutil import encodable, write_text_atomic
from k3code.learning.updateconfig import merge_patch
from k3code.redact import scrub_text

logger = logging.getLogger(__name__)

DAY = 86400.0
WEEK = 7 * DAY
#: After a maintenance pass that failed, the next try waits this long (it used to be tried again at every turn end).
RETRY_AFTER = 3600.0
#: A stored time further ahead than this is a wrong clock's doing, not a pass that is not due yet.
CLOCK_SLACK = 60.0
#: Sessions whose last tool failure is remembered (the oldest is dropped), and what is kept of the failed call.
MAX_FAILURES = 256
ARG_KEEP = 256
BASH_KEEP = 4000


def _slim_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """The failed call's arguments as ``followup_hint`` needs them: it compares them with the retry's, so a long
    value (a 100 KB ``content``) is replaced by its digest, and the retry's are reduced the same way."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if tool == "bash" and k == "command":
            out[k] = str(v)[:BASH_KEEP]
        elif v is None or isinstance(v, (bool, int, float)):
            out[k] = v
        else:
            raw = v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)
            digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]
            out[k] = raw if len(raw) <= ARG_KEEP else f"#{digest}"
    return out


def _unfreeze(session: Any, *keys: str) -> None:
    """Let the session's prompt read ``keys`` afresh at its next turn: the user accepted a proposal that writes them,
    and it should apply to this conversation, not wait for a compaction (background writers stay frozen out)."""
    frozen = getattr(session, "prompt_frozen", None)
    for key in keys if frozen is not None else ():
        frozen.pop(key, None)


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
        #: (session id, tool) -> (tool_error row id, signature, the failed call's args, slimmed): waits for the retry
        #: that works; the newest MAX_FAILURES only (a session's entries also go when it closes)
        self._last_failure: OrderedDict[tuple[str, str], tuple[int, str, dict[str, Any]]] = OrderedDict()
        self._maintaining = False
        #: cwd -> the approval/plan rows seen by the last permission mining there
        self._mined: dict[str, tuple[int, int]] = {}

    # ── config / state ──

    @property
    def cfg(self) -> dict[str, Any]:
        return learning_cfg(self.server.config)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg["enabled"])

    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _set_state(self, **kv: Any) -> None:
        write_text_atomic(self._state_path, json.dumps({**self._state(), **kv}))

    def spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:  # nothing else would ever look at it
            logger.warning("learning task failed", exc_info=(type(exc), exc, exc.__traceback__))

    async def aclose(self, timeout: float = 5.0) -> None:
        """Cancel the spawned tasks, wait for them, close the decision log: the gateway calls it when it closes."""
        pending = [t for t in self._tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        self.log.close()

    def forget_session(self, session_id: str) -> None:
        """A closed session will not retry anything: drop the failures it was waiting on."""
        for key in [k for k in self._last_failure if k[0] == session_id]:
            del self._last_failure[key]

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
        await self.mine_if_changed(session)

    def tool_outcome(self, session: Any, call: Any, result: dict[str, Any], failure: Any) -> str | None:
        """Record a failed tool call as a ``tool_error``; the next call of that tool in the session that looks like a
        retry and works becomes the row's hint. A signature seen REPEATS times in the project within WINDOW proposes
        a project gotcha. Returns a reminder for the model when the failure already has a lesson (the agent loop sends
        it after the step's tool results), else None. Never raises: it runs inside the turn."""
        if not self.enabled or session is None:
            return None
        try:
            return self._tool_outcome(session, call, result, failure)
        except Exception:  # noqa: BLE001 - learning must never break a turn
            logger.warning("tool error learning failed", exc_info=True)
            return None

    def _tool_outcome(self, session: Any, call: Any, result: dict[str, Any], failure: Any) -> str | None:
        cwd = str(session.perms.cwd)
        key = (session.session_id, call.name)
        if failure is None:
            pending = self._last_failure.pop(key, None)
            if pending is not None:
                row_id, sig, failed_args = pending
                hint = gotchas.followup_hint(call.name, failed_args, _slim_args(call.name, call.arguments))
                if hint:
                    self.log.update_detail(row_id, followup=hint)
                    self._maybe_gotcha(session, cwd, call.name, sig)
            return None
        if failure.error_class in gotchas.NOT_PROJECT or (
            failure.error_class.startswith("exit ") and not str(result.get("stderr") or "").strip()
        ):
            return None  # a failing test run or `grep` without a match says nothing about the project by itself
        # a non-UTF-8 file name decodes to a lone surrogate: made encodable here, once, it matches the logged row and
        # reaches no card, notification or model request as text that cannot be sent
        sig = encodable(failure.signature)
        row_id = self.log.record(
            "tool_error",
            session=session.session_id,
            cwd=cwd,
            subject=sig,
            choice=failure.error_class,
            detail={"tool": call.name, "error": failure.first_line},
            actor="auto" if getattr(session, "background", False) else "user",
        )
        self._last_failure[key] = (row_id, sig, _slim_args(call.name, call.arguments))
        self._last_failure.move_to_end(key)
        while len(self._last_failure) > MAX_FAILURES:
            self._last_failure.popitem(last=False)
        hint = encodable(self._maybe_gotcha(session, cwd, call.name, sig))
        return self._known_fix(cwd, call.name, sig, hint)

    def _known_fix(self, cwd: str, tool: str, sig: str, hint: str) -> str | None:
        """The reminder for a failure with a lesson: the project's accepted gotcha line, else its auto-learned one,
        else the user-level one, else the working retry recorded on an earlier row in the window (``hint``). Small
        file reads, no model call."""
        pid = self.log.project_for(cwd)  # cached per cwd
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
        rows = [
            r
            for r in self.log.query("tool_error", project=pid, since=self.clock() - gotchas.WINDOW, actor=None)
            if r["subject"] == sig and r["detail"].get("tool") == tool
        ]
        hint = next((str(r["detail"]["followup"]) for r in reversed(rows) if r["detail"].get("followup")), "")
        line = f"{tool}: {sig}" + (f" — {hint}" if hint else "")
        if (
            self.cfg["auto_gotchas"]
            and len(rows) >= gotchas.AUTO_REPEATS
            and gotchas.auto_ok(str(rows[-1]["choice"]), hint)
        ):
            self._auto_gotcha(session, pid, tool, sig, hint, line)
            return hint
        if len(rows) >= gotchas.REPEATS:
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
            key=dedup_key("project_gotcha", f"{pid} {tool} {sig}"),
        )
        if p is not None:
            self.emit(session, [p])

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
        # notification.show is the event the TUI shows; "notification" had no handler and the notice was dropped
        session.emit(
            "notification.show",
            {"session_id": session.session_id, "text": text, "level": "info", "kind": "ttl", "ttl_ms": 15000},
        )

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

    def _mine(self, log: Any, cwd: str) -> list[permrules.Candidate]:
        c = self.cfg
        return permrules.mine(
            log,
            min_approvals=int(c["perm_min_approvals"]),
            min_denials=int(c["perm_min_denials"]),
            user_projects=int(c["user_scope_projects"]),
            cwd=cwd,
        )

    def mine_permissions(self, session: Any) -> list[Proposal]:
        cands = self._mine(self.log, str(session.perms.cwd) if session is not None else "")
        made = permrules.to_proposals(cands, self.store, session.session_id if session else "")
        if session is not None:
            self.emit(session, made)
        return made

    def _mine_in_thread(self, cwd: str) -> list[permrules.Candidate]:
        reader = self.log.reader()  # its own connection: the log's stays on the event loop's thread
        try:
            return self._mine(reader, cwd)
        finally:
            reader.close()

    async def mine_if_changed(self, session: Any) -> list[Proposal]:
        """``mine_permissions`` off the event loop (it scans the whole approval history: ~0.7 s at 50 000 rows), and
        only when approvals or plan decisions were recorded since the last time here: the same history yields the
        same candidates, which the proposal store would drop as seen."""
        cwd = str(session.perms.cwd) if session is not None else ""
        mark = self.log.mark(("approval", "plan"))
        if self._mined.get(cwd) == mark:
            return []
        self._mined[cwd] = mark  # before the await: a second caller meanwhile has nothing new to look at
        cands = await asyncio.to_thread(self._mine_in_thread, cwd)
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
            _unfreeze(session, "gotchas", "machine_gotchas")
            return f"added to the known pitfalls of this project ({path})"
        if p.kind == "preference" and payload.get("text"):
            from k3code.memory import user_memory_path

            distiller.add_user_line(user_memory_path(), str(payload["text"]))
            _unfreeze(session, "user_memory")
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
        """Keep one finished turn's inputs and outcome for the replay harness. Never raises: it runs at turn end.
        Only the optimizer reads the records, so nothing is kept (or built: it renders the skills and memory prompts
        and appends to a file of up to 40 MB) while ``learning.optimizer.enabled`` is off."""
        if not self.enabled or not self.cfg["optimizer"]["enabled"]:
            return
        try:
            from k3code.memory import memory_prompt
            from k3code.skills import PROMPT_LIMIT, skills_prompt

            cwd = session.perms.cwd
            ctx = self.server.config.context or {}
            tok_in, tok_out = self.server.usage.turn_tokens(turn_id) if turn_id else (0, 0)
            skill_limit = int(ctx.get("skill_prompt_limit", PROMPT_LIMIT))  # the limit the system prompt is built with
            skill_text = skills_prompt(cwd, list(self.server.config.skills.roots), limit=skill_limit)
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
                skill_lines=[
                    len(ln) for ln in skill_text.splitlines() if ln.startswith("- ") and not ln.startswith("- …")
                ],
                ts=self.clock(),
            )
            self.replays.append(record)
        except Exception:  # noqa: BLE001 - recording must never break a turn
            logger.warning("replay record failed", exc_info=True)

    async def turn_finished(self, session: Any, status: str) -> None:
        if not self.enabled:
            return
        await self.mine_if_changed(session)
        msgs = session.stored.messages
        turns = review.user_turns(msgs)
        meta = session.stored.meta
        minimum = int(self.cfg["review_min_turns"])
        unattended = bool(getattr(session, "background", False))  # loop ticks, cron, automations: synthetic prompts
        if (
            status == "done"
            and not unattended
            and turns > minimum
            and turns > int(meta.get("reviewed_turns", 0)) + minimum - 1
        ):
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
            emit("notification.show", {"text": text, "level": "info", "kind": "ttl", "ttl_ms": 15000})

    async def maintenance(self, session: Any = None, *, force: bool = False) -> dict[str, Any]:
        """Daily distill + curate, weekly optimizer (opt-in). Cheap tier; safe to call often.

        One pass at a time (two sessions ending together each made the model calls), the attempt is stamped before the
        work and a failed one is not tried again for RETRY_AFTER (it was, at every turn end, calling the model each
        time), and a stored time in the future (a clock that was wrong) counts as never."""
        if self._maintaining:
            return {"skipped": "a pass is already running"}
        self._maintaining = True
        try:
            return await self._maintenance(session, force)
        finally:
            self._maintaining = False

    def _due(self, st: dict[str, Any], key: str, every: float, now: float, force: bool) -> bool:
        def seen(name: str) -> float:
            try:
                t = float(st.get(name) or 0)
            except (TypeError, ValueError):
                return 0.0
            return 0.0 if t > now + CLOCK_SLACK else t

        return force or (now - seen(key) > every and now - seen(f"{key}_attempted") > RETRY_AFTER)

    async def _maintenance(self, session: Any, force: bool) -> dict[str, Any]:
        now = self.clock()
        st = self._state()
        done: dict[str, Any] = {}
        if self._due(st, "distilled", DAY, now, force):
            from k3code.memory import user_memory_path

            self._set_state(distilled_attempted=now)
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
        if self.cfg["optimizer"]["enabled"] and self._due(st, "optimized", WEEK, now, force):
            self._set_state(optimized_attempted=now)
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
        # off the event loop: the replay is CPU work (a 40 MB file to read, seconds of fake-provider requests) and the
        # worker thread has a loop of its own; only the newest turns are read and replayed
        turns = await asyncio.to_thread(self.replays.load, replay.MAX_REPLAY_TURNS)
        cands = await asyncio.to_thread(lambda: asyncio.run(replay.token_candidates(context, turns)))
        # a candidate the A/B window already judged worse and rolled back is never auto-applied again: it goes to
        # the proposal store, whose dedup key latches it for the human path
        for c in cands:
            if c["auto_ok"] and self._rolled_back_before(c["patch"]):
                c["auto_ok"] = False
        for c in cands:
            if c["auto_ok"] and not self._already_in_effect(c["patch"]):
                self._auto_apply(c)
        made += optimizer.propose_replayed([c for c in cands if not c["auto_ok"]], self.store)
        if session is not None:
            self.emit(session, made)
        return made

    def _rolled_back_before(self, patch: dict[str, Any]) -> bool:
        return any(
            x["patch"] == patch and x["status"] == "rolled_back"
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
            f"Optimizer applied automatically (experiment {exp['id']}): {cand['title']}. Replay: tokens "
            f"-{cand['evidence']['token_reduction_pct']}%, pass drop at most "
            f"{cand['evidence']['pass_drop_points']} points. Rolled back automatically if worse."
        )
