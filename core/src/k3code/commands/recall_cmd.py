"""/recall <query> [--project] [--since 90d] [--session ID] | /recall read <session_id>:<seq>: search what earlier
sessions said (the recall archive, k3code.recall) and read around a hit. Plain text for the user, every string scrubbed
of credentials; the search runs on a connection of its own in a worker thread, never the loop's store connection."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from k3code import recall
from k3code.commands import CommandDef
from k3code.commands._util import ArgError, pop_flag, pop_option, reply, session_cwd, split_args
from k3code.extratools import MAX_READ_MESSAGE_CHARS, recall_scope_default
from k3code.memory import identity_root
from k3code.redact import scrub_text

USAGE = "Usage: /recall <query> [--project] [--since 90d|2026-03-01] [--session ID] | /recall read <session_id>:<seq>"
RESUME_HINT = "resume a session with /resume <session_id>"
LIMIT = 10  # hits shown
READ_BEFORE, READ_AFTER = 1, 4  # messages /recall read shows before and after the one asked for


def _usage(problem: str) -> dict[str, Any]:
    return reply(scrub_text(f"{problem}. {USAGE}"))


def _hit_data(hit: recall.Hit) -> dict[str, Any]:
    return {
        "id": hit.id,
        "session_id": hit.session_id,
        "seq": hit.seq,
        "title": scrub_text(hit.title or ""),
        "cwd": scrub_text(hit.cwd or ""),
        "at": hit.at,
        "approximate": hit.approximate,
        "role": hit.role,
        "kind": hit.kind,
        "snippet": scrub_text(hit.snippet),
    }


class RecallCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="recall",
            help="Search earlier sessions: /recall <query> [--project] [--since 90d] [--session ID]"
            " | /recall read <session_id>:<seq>",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        try:
            args = split_args(arg)
            if args and args[0] == "read":
                return await self._read(ctx, args[1:])
            project_only = pop_flag(args, "--project")
            since_text = pop_option(args, "--since")
            only = pop_option(args, "--session")
        except ArgError as e:
            return _usage(str(e))
        flags = [a for a in args if a.startswith("--")]
        if flags:
            return _usage(f"unknown option {flags[0][:40]}")
        query = " ".join(args).strip()
        if not query:
            return _usage("/recall needs words to look for")
        try:
            since = recall.parse_since(since_text) if since_text is not None else None
        except ValueError as e:
            return _usage(str(e))
        if only is not None:
            only = only.split(":", 1)[0].strip()
            if not only or not ctx.store.summaries([only]):
                return _usage(f"unknown session: {only[:80]!r}")
        scope = "project" if project_only else recall_scope_default()
        where = session_cwd(ctx, session_id)

        def run() -> recall.Found:
            project = str(identity_root(where))
            return recall.search_file(
                ctx.store.path,
                query,
                project=project if scope == "project" else None,
                current_project=project,
                since=since,
                session_id=only,
                limit=LIMIT,
            )

        try:
            found = await asyncio.to_thread(run)
        except sqlite3.Error as e:
            return reply(scrub_text(f"Recall index unavailable: {e}"))
        scope_text = "this project only" if scope == "project" else "all projects, this one first"
        data = {"scope": scope, "indexed": found.indexed, "pending": found.pending}
        if not found.hits:
            text = f"No matches for {query[:80]!r} in earlier sessions ({scope_text}; {recall.index_note(found)})."
            if project_only:
                text += " Drop --project to search every project."
            elif scope == "project":
                text += " Your config sets recall.scope to project; set it to all to search every project."
            return reply(scrub_text(text), results=[], **data)
        lines = [f"{len(found.hits)} hits ({scope_text}; {recall.index_note(found)})"]
        lines += [recall.hit_lines(hit, current_session=session_id or "") for hit in found.hits]
        lines += ["read around a hit with /recall read <id>", RESUME_HINT]
        return reply(scrub_text("\n".join(lines)), results=[_hit_data(h) for h in found.hits], **data)

    async def _read(self, ctx: Any, args: list[str]) -> dict[str, Any]:
        if len(args) != 1:
            return _usage("/recall read needs one id: <session_id>:<seq> from a /recall hit")
        ref = args[0][:200]
        sid, _, seq_text = ref.partition(":")
        try:
            seq = int(seq_text) if seq_text else None
        except ValueError:
            return _usage(f"unknown id: {ref[:80]!r}")
        try:
            got = await asyncio.to_thread(recall.read_file, ctx.store.path, sid.strip(), seq, READ_BEFORE, READ_AFTER)
        except sqlite3.Error as e:
            return reply(scrub_text(f"Recall index unavailable: {e}"))
        if not got:
            return _usage(f"unknown id: {ref[:80]!r} (ids come from /recall)")
        blocks: list[str] = []
        for m in got:
            text = scrub_text(m.text)
            text = text if len(text) <= MAX_READ_MESSAGE_CHARS else text[:MAX_READ_MESSAGE_CHARS] + "…(cut)"
            role = f"{m.role} (summary)" if m.kind == "summary" else m.role
            blocks.append(f"[{m.id}] {role} · {recall.day(m.at, m.approximate)}\n{text}")
        last = got[-1]
        blocks.append(f"more: /recall read {last.session_id}:{last.seq + 1}\n{RESUME_HINT}")
        results = [
            {"id": m.id, "session_id": m.session_id, "seq": m.seq, "role": m.role, "kind": m.kind, "at": m.at}
            | {"approximate": m.approximate, "text": scrub_text(m.text)}
            for m in got
        ]
        return reply(scrub_text("\n\n".join(blocks)), results=results)
