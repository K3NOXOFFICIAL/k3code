"""/memory [add <text> [--user] | edit [--user] | mem0 <query>]."""

from __future__ import annotations

import os
from typing import Any

import httpx

from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, session_cwd, split_args
from k3code.memory import append_memory, project_memory_path, user_memory_path


async def mem0_search(cfg: Any, query: str, *, client: httpx.AsyncClient | None = None) -> list[str]:
    key = os.environ.get(cfg.mem0.api_key_env, "") if cfg.mem0.api_key_env else ""
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
            help="List memory files; /memory add <text> [--user] | edit [--user] | mem0 <query>",
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
        rows = []
        for scope, p in (("user", usr), ("project", proj)):
            size = f"{p.stat().st_size} bytes" if p.is_file() else "(missing)"
            size_n = p.stat().st_size if p.is_file() else 0
            rows.append({"scope": scope, "path": str(p), "exists": p.is_file(), "size": size_n})
            rows[-1]["label"] = f"{scope}: {p}  {size}"
        return reply("\n".join(r["label"] for r in rows), files=rows)
