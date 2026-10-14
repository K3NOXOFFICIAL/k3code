"""/memory [show | add <text> [--user] | edit [--user] | mem0 <query>]."""

from __future__ import annotations

from typing import Any

import httpx

from k3code.commands import CommandDef
from k3code.commands._util import reply, session_cwd, split_args
from k3code.config import env_value
from k3code.memory import (
    _user_body,
    append_memory,
    learned_notes_path,
    load_memory,
    project_memory_path,
    read_learned,
    user_memory_path,
)

USAGE = "Usage: /memory | show | add <text> [--user] | edit [--user] | mem0 <query>"
SHOW_CLIP = 4000  # characters /memory show prints of each part


def _clip(text: str) -> str:
    return text if len(text) <= SHOW_CLIP else text[:SHOW_CLIP] + f"\n…(clipped; {len(text) - SHOW_CLIP} more chars)"


def show_memory(cwd: Any) -> dict[str, Any]:
    """What the model is given: the memory files, the learned notes and how many gotchas the project has."""
    from k3code.learning.decisions import project_id
    from k3code.learning.gotchas import _entries, auto_gotchas_path, gotchas_path, user_gotchas_path

    parts: list[str] = []
    files = []
    for m in load_memory(cwd):
        text = _user_body(m.text) if m.scope == "user" else m.text
        files.append({"scope": m.scope, "path": str(m.path), "chars": len(text)})
        parts.append(f"## {m.scope}: {m.path}\n{_clip(text)}")
    if not files:
        parts.append("## memory files: none")
    notes = read_learned(cwd)
    body = "\n".join(f"- {f}" for f in notes) or "(none)"
    parts.append(f"## learned notes ({len(notes)}): {learned_notes_path(cwd)}\n{_clip(body)}")
    pid = project_id(cwd)
    gotchas = {
        "accepted": len(_entries(gotchas_path(pid))),
        "auto": len(_entries(auto_gotchas_path(pid))),
        "user": len(_entries(user_gotchas_path())),
    }
    parts.append(
        f"## gotchas: {gotchas['accepted']} accepted (system prompt), {gotchas['auto']} auto-learned (in-turn "
        f"reminders only), {gotchas['user']} on this machine"
    )
    return reply("\n\n".join(parts), files=files, learned=notes, gotchas=gotchas)


async def mem0_search(cfg: Any, query: str, *, client: httpx.AsyncClient | None = None) -> list[str]:
    key = env_value(cfg.mem0.api_key_env) if cfg.mem0.api_key_env else ""  # the environment, else the setup env file
    headers = {"Authorization": f"Token {key}", "X-API-Key": key} if key else {}
    body: dict[str, Any] = {"query": query, "limit": 8}
    if cfg.mem0.user_id:
        body["user_id"] = cfg.mem0.user_id
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(cfg.mem0.url.rstrip("/") + "/search", json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if own:
            await client.aclose()
    items = data.get("results", data) if isinstance(data, dict) else data
    return [str(i.get("memory") or i.get("text") or i) if isinstance(i, dict) else str(i) for i in items or []]


class MemoryCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="memory",
            help="List memory files; /memory show (what the model sees) | add <text> [--user] | edit [--user] | "
            "mem0 <query>",
        )

    @staticmethod
    def _read_again(ctx: Any, session_id: str | None, user: bool, *, learned: bool = False) -> None:
        """The session's prompt holds the memory files as it first saw them, against the daily distill's rewrites and
        edits made outside; a line added or a file edited with the command is the user's own, meant to apply now.
        ``learned``: the learned notes too (an /import merged some)."""
        live = ctx.sessions.get(session_id) if session_id and hasattr(ctx, "sessions") else None
        if live is not None:
            live.prompt_frozen.pop("user_memory" if user else "project_memory", None)
            if learned:
                live.learned_notes = None

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        # --user is a flag first or last (or right after the sub-command); anywhere else it is part of the text
        user = False
        if args and args[0] == "--user":
            user = True
            args = args[1:]
        elif len(args) > 1 and args[-1] == "--user":
            user = True
            args = args[:-1]
        elif len(args) > 2 and args[1] == "--user" and args[0] == "add":
            user = True
            del args[1]
        cwd = session_cwd(ctx, session_id)
        proj = project_memory_path(cwd)
        usr = user_memory_path()
        target = usr if user else proj
        sub = args[0] if args else ""
        if sub == "add":
            text = " ".join(args[1:]).strip()
            if not text:
                return reply("Usage: /memory add <text> [--user]")
            append_memory(target, text)
            self._read_again(ctx, session_id, user)
            return reply(f"Added to {target}", path=str(target))
        if sub == "edit":
            # the user is about to change the file (a turn between this and the save reads the old text once more)
            self._read_again(ctx, session_id, user)
            return reply(f"Edit {target} (the CLI opens $EDITOR; here, open it yourself).", path=str(target))
        if sub == "show":
            return show_memory(cwd)
        if sub == "mem0":
            query = " ".join(args[1:]).strip()
            if not query:
                return reply("Usage: /memory mem0 <query>")
            if not ctx.config.mem0.url:
                return reply("mem0 isn't configured (set mem0.url and mem0.api_key_env in config).")
            try:
                hits = await mem0_search(ctx.config, query)
            except (httpx.HTTPError, ValueError) as e:
                return reply(f"mem0 search failed: {e}")
            return reply("\n".join(f"- {h}" for h in hits) or "No mem0 results.", results=hits)
        if sub:
            return reply(USAGE)
        rows = []
        for scope, p in (("user", usr), ("project", proj)):
            size = f"{p.stat().st_size} bytes" if p.is_file() else "(missing)"
            size_n = p.stat().st_size if p.is_file() else 0
            rows.append({"scope": scope, "path": str(p), "exists": p.is_file(), "size": size_n})
            rows[-1]["label"] = f"{scope}: {p}  {size}"
        return reply("\n".join(r["label"] for r in rows), files=rows)
