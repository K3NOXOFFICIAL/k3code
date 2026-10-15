"""/memory [add <text> [--user] | edit [--user] | mem0 <query>].

``/memory`` lists every file the prompt's memory section is built from for the session's directory, in the order the
prompt reads them: the loader's own scan (``k3code.memory.plan_memory``), so the two cannot disagree."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from k3code import memory as mem
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, session_cwd, split_args
from k3code.memory import append_memory, project_memory_path, user_memory_path


async def mem0_search(cfg: Any, query: str, *, client: httpx.AsyncClient | None = None) -> list[str]:
    key = os.environ.get(cfg.mem0.api_key_env, "") if cfg.mem0.api_key_env else ""
    headers = {"Authorization": f"Token {key}", "X-API-Key": key} if key else {}
    body: dict[str, Any] = {"query": query, "limit": 8, **cfg.mem0.identifiers()}
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


_CUT = {
    "": "",
    "file": ", cut at the {limit}-character per-file limit",
    "total": ", cut short: the {total}-character total ran out",
    "dropped": ", left out: the {total}-character total ran out",
}
_WHY = {
    mem.MISSING: "(missing)",
    mem.EMPTY: "(empty)",
    mem.SAME: "(same text as {same_as}, not repeated)",
    mem.REFUSED: "(not read: it leads outside the allowed directories or to a credential location)",
    mem.UNREADABLE: "(unreadable)",
    mem.OFF: "(not in the prompt: learning.enabled is false)",
}


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _note(s: mem.MemorySource, size: int | None, limit: int, total: int) -> str:
    """What the listing says about one source: its size and how the caps cut it, or why it gives nothing."""
    if s.status != mem.READ:
        return _WHY[s.status].format(same_as=s.same_as)
    return ("size unknown" if size is None else f"{size} bytes") + _CUT[s.cut].format(limit=limit, total=total)


def memory_listing(cwd: Path, config: Any) -> tuple[str, list[dict[str, Any]]]:
    """The files the system prompt's memory section is built from for ``cwd``, in prompt order, as text and rows.

    It lists ``plan_memory`` (the loader's own scan with the caps the prompt applies) and the learned notes, so it
    names exactly the files the prompt reads and says which ones the caps cut. Missing, empty and repeated files are
    listed too: they are where the loader looks. The files @imports pulled in follow the file that imports them.
    """
    from k3code.learning import learning_cfg

    limit, total = mem.memory_limit(config), mem.MAX_MEMORY_TOTAL
    proj, usr = project_memory_path(cwd), user_memory_path()
    sources = mem.plan_memory(cwd, limit, total)
    sources.append(mem.learned_source(cwd, limit, enabled=bool(learning_cfg(config)["enabled"])))
    rows: list[dict[str, Any]] = []
    lines = [f"Memory files for {cwd}, in prompt order (each cut at {limit} characters, {total} in all):"]
    for s in sources:
        size = _size(s.path)
        target = "project" if s.path == proj else "user" if s.path == usr else ""
        label = f"{s.scope}: {s.path}  {_note(s, size, limit, total)}"
        imports = [str(p) for p in s.file.imports] if s.file is not None else []
        rows.append(
            {
                "scope": s.scope,
                "path": str(s.path),
                "exists": size is not None,
                "size": size or 0,
                "status": s.status,
                "cut": s.cut,
                "imports": imports,
                "edit_target": target,
                "label": label,
            }
        )
        lines.append(label)
        lines += [f"    @import {p}" for p in imports]
    lines.append(f"/memory add and /memory edit write to {proj}; with --user, to {usr}.")
    return "\n".join(lines), rows


class MemoryCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="memory",
            help="List the memory files the prompt reads here, in order; "
            "/memory add <text> [--user] | edit [--user] | mem0 <query>",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        user = pop_flag(args, "--user")
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
            return reply(f"Added to {target}", path=str(target))
        if sub == "edit":
            return reply(f"Edit {target} (the CLI opens $EDITOR; here, open it yourself).", path=str(target))
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
            return reply("Usage: /memory | add <text> [--user] | edit [--user] | mem0 <query>")
        text, rows = memory_listing(cwd, ctx.config)
        return reply(text, files=rows)
