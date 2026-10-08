"""/settings: structured read-only overview (TUI shows it as an overlay)."""

from __future__ import annotations

from typing import Any

from k3code import outputstyle
from k3code.commands import CommandDef
from k3code.commands._util import reply, session_cwd
from k3code.memory import project_memory_path, user_memory_path
from k3code.paths import home, project_config_path, user_config_path


def build_settings_view(ctx: Any, session_id: str | None) -> dict[str, Any]:
    cfg = ctx.config
    cwd = session_cwd(ctx, session_id)
    live = ctx.sessions.get(session_id) if session_id else None
    chain = []
    for p in cfg.providers:
        spec = p.models.get(cfg.default_model) or p.models.get("default") or next(iter(p.models.values()), "")
        chain.append({"provider": p.name, "models": spec if isinstance(spec, list) else [spec]})
    rules = 0
    mode = cfg.permission_mode
    if live is not None:
        live.perms.cwd = cwd
        live.perms.reload()
        p = live.perms
        rules = len(p.user_rules) + len(p.project_rules) + len(p.session_rules)
        mode = p.mode.value
    style = (live.stored.meta.get("output_style") if live else None) or cfg.output_style
    return {
        "model_key": (live.stored.model if live and live.stored.model else cfg.default_model),
        "model_chain": chain,
        "effort": (live.reasoning_effort if live else None) or "default",
        "permission_mode": mode,
        "permission_rules": rules,
        "focus_mode": cfg.display.focus_mode,
        "theme": cfg.display.theme or "default",
        "output_style": style,
        "output_styles": outputstyle.available(cwd),
        "providers": [
            {
                "name": p.name,
                "kind": p.kind,
                "base_url": p.base_url,
                "api_key_env": p.api_key_env,
                "key_set": bool(p.api_key) or p.kind == "claude-cli",  # claude-cli uses the local Claude Code login
            }
            for p in cfg.providers
        ],
        "mcp_servers": sorted(cfg.mcp.servers),
        "paths": {
            "home": str(home()),
            "user_config": str(user_config_path()),
            "project_config": str(project_config_path(cwd)),
            "sessions_db": str(getattr(ctx.store, "path", "")),
            "user_memory": str(user_memory_path()),
            "project_memory": str(project_memory_path(cwd)),
            "cwd": str(cwd),
        },
    }


def render_text(v: dict[str, Any]) -> str:
    chain = " → ".join(p["provider"] + ":" + ",".join(p["models"]) for p in v["model_chain"]) or "no providers"
    provs = ", ".join(
        f"{p['name']} [claude-cli] uses the Claude Code login"
        if p["kind"] == "claude-cli"
        else f"{p['name']} [{p['kind']}] key {'set' if p['key_set'] else 'MISSING'} ({p['api_key_env']})"
        for p in v["providers"]
    )
    lines = [
        f"model:        {v['model_key']}  ({chain})",
        f"effort:       {v['effort']}",
        f"permissions:  {v['permission_mode']}  ({v['permission_rules']} rule(s))",
        f"focus mode:   {'on' if v['focus_mode'] else 'off'}",
        f"theme:        {v['theme']}",
        f"output style: {v['output_style']}",
        "providers:    " + (provs or "none"),
        "paths:",
        *(f"  {k}: {p}" for k, p in v["paths"].items()),
        "hint: re-run any setup step with `k3code setup --step <name>` (providers, tiers, permissions, theme, ...)",
    ]
    return "\n".join(lines)


class SettingsCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="settings", help="Show model chain, effort, permissions, style, providers and paths")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        view = build_settings_view(ctx, session_id)
        return reply(render_text(view), type="settings", settings=view)


class FocusCommand(CommandDef):
    """Gateway twin of the TUI's /focus: shows in /help and toggles ``display.focus`` (``config.set``)."""

    def __init__(self) -> None:
        super().__init__(name="focus", help="Toggle focus mode: only prompts, errors, final answers [on|off|status]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        mode = arg.strip().lower()
        cur = bool(ctx.config.display.focus_mode)
        if mode in ("", "toggle"):
            nxt = not cur
        elif mode in ("on", "off"):
            nxt = mode == "on"
        elif mode in ("status", "show", "?"):
            return reply(f"focus view {'on' if cur else 'off'}", focus=cur)
        else:
            return reply("usage: /focus [on|off|status]")
        ctx.config.display.focus_mode = nxt
        return reply(f"focus view {'enabled' if nxt else 'disabled'}", focus=nxt)
