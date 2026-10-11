"""/maxagents: how many sub-agents one /ultracode run may start, how many run at once, and dynamic sizing.

Also reachable as ``/ultracode agents ...`` and ``/ultracode dynamic ...`` (see ``ultra_cmd``). The setting belongs to
the session unless ``--save`` is given: raising the number of agents raises what a run can spend, so it never persists
by accident.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from k3code import confio
from k3code.autonomy.agents import (
    AUTO,
    AUTO_CEILING,
    HARD_MAX_PARALLEL,
    MAX_AGENTS_CAP,
    AgentLimits,
    agent_limits,
    clean_override,
    session_limits,
    whole_number,
)
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, split_args
from k3code.config import default_project_dir
from k3code.paths import project_config_path, user_config_path

USAGE = (
    "Usage: /maxagents [status | <1-64> | auto | parallel <1-8> | dynamic on|off | reset] [--save]\n"
    "  <N>, auto        most sub-agents one /ultracode run may start in all (auto: it picks, up to "
    f"{AUTO_CEILING})\n"
    "  parallel <N>     most sub-agents of this session running at the same time\n"
    "  dynamic on|off   size the planners and workers from the task (never above the numbers above)\n"
    "  reset            drop this session's own settings\n"
    "  --save           keep it in your config.yaml; without it the setting is for this session only"
)

#: config path of each setting (a dotted key in config.yaml)
_KEYS = {
    "max_agents": ("ultracode", "max_agents"),
    "max_parallel": ("autonomy", "agents", "max_parallel"),
    "dynamic": ("autonomy", "agents", "dynamic"),
}


class AgentLimitsError(ValueError):
    """The argument is no setting /maxagents knows; the message says what is wrong."""


def parse(args: list[str]) -> dict[str, Any] | None:
    """The settings ``args`` change (``max_agents`` / ``max_parallel`` / ``dynamic``); None for ``reset``, {} for
    ``status``. Raises :class:`AgentLimitsError` on anything else."""
    if not args or args[0].lower() == "status":
        return {}
    word = args[0].lower()
    if word == "reset":
        return None
    if word == "parallel" and len(args) == 2:
        n = whole_number(_int(args[1]), lo=1, hi=HARD_MAX_PARALLEL)
        if n is None:
            raise AgentLimitsError(f"parallel: a whole number from 1 to {HARD_MAX_PARALLEL} (got {args[1]!r})")
        return {"max_parallel": n}
    if word == "dynamic" and len(args) == 2:
        value = args[1].lower()
        if value in ("on", "true", "yes", "enable"):
            return {"dynamic": True}
        if value in ("off", "false", "no", "disable"):
            return {"dynamic": False}
        if value == "status":
            return {}
        raise AgentLimitsError(f"dynamic: on or off (got {args[1]!r})")
    if len(args) == 1 and word == AUTO:
        return {"max_agents": AUTO}
    if len(args) == 1:
        n = whole_number(_int(word), lo=1, hi=MAX_AGENTS_CAP)
        if n is None:
            raise AgentLimitsError(f"{args[0]}: a whole number from 1 to {MAX_AGENTS_CAP}, or auto")
        return {"max_agents": n}
    raise AgentLimitsError(f"Unknown setting: {' '.join(args)}")


_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def alias_args(arg: str) -> str | None:
    """``/ultracode agents <N|auto|status|reset>``, ``agents parallel <N>`` and ``dynamic <on|off|status>`` as
    /maxagents arguments, or None when ``arg`` is anything else. Only the whole argument counts:
    ``agents should review X`` is a task. A number that is out of range is still a settings line (the usage message
    answers it), never a task."""
    words = split_args(arg)
    save = ["--save"] if "--save" in words else []
    words = [w for w in words if w not in ("--save", "--session")]  # --session is what a setting is anyway
    if not words:
        return None
    head, rest = words[0].lower(), [w.lower() for w in words[1:]]
    if head == "agents":
        one = len(rest) == 1 and (rest[0] in (AUTO, "status", "reset") or _NUMBER.match(rest[0]) is not None)
        parallel = len(rest) == 2 and rest[0] == "parallel" and _NUMBER.match(rest[1]) is not None
        return " ".join([*rest, *save]) if (not rest or one or parallel) else None
    if head == "dynamic" and len(rest) <= 1 and (not rest or rest[0] in ("on", "off", "status")):
        return " ".join(["dynamic", *(rest or ["status"]), *save])
    return None


def _int(text: str) -> Any:
    try:
        return int(text)
    except ValueError:
        return None


def _set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[path[-1]] = value


def write_user_config(changes: dict[str, Any]) -> tuple[Any, Any]:
    """Save ``changes`` into the user's config.yaml (validated first, the old file kept as a backup)."""
    path = user_config_path()
    data = confio.read_yaml(path)
    for key, value in changes.items():
        _set_path(data, _KEYS[key], value)
    confio.validate(data)
    return path, confio.write_yaml(path, data)


def project_sections() -> list[str]:
    """The sections among ``ultracode`` and ``autonomy`` that the gateway's trusted project config sets: such a section
    replaces the user's whole section, so the settings made here for it would not take effect."""
    from k3code import trust

    try:
        text = trust.trusted_text(default_project_dir())
        data = yaml.safe_load(text) if text else None
    except Exception:  # noqa: BLE001 - a hint only
        return []
    return [k for k in ("ultracode", "autonomy") if isinstance(data, dict) and isinstance(data.get(k), dict)]


def payload(limits: AgentLimits, live: int = 0) -> dict[str, Any]:
    return {
        "max_agents": limits.describe_max_agents() if limits.auto else limits.max_agents,
        "dynamic": limits.dynamic,
        "max_parallel": limits.max_parallel,
        "max_nested": limits.max_nested,
        "max_spawns": limits.max_spawns,
        "fanout_width": limits.fanout_width,
        "sources": dict(limits.sources),
        "live": live,
    }


def describe(limits: AgentLimits, live: int = 0) -> str:
    """The status paragraph: each limit, where it came from, and what it controls."""

    def src(name: str) -> str:
        where = limits.sources.get(name, "default")
        return "" if where == "default" else f" ({where})"

    agents = AUTO if limits.auto else str(limits.max_agents)
    auto_note = f" up to {AUTO_CEILING}, trimmed to what the plan needs" if limits.auto else ""
    lines = [
        f"max agents per /ultracode run: {agents}{auto_note}{src('max_agents')}",
        f"dynamic sizing: {'on' if limits.dynamic else 'off'}{src('dynamic')}"
        + (
            " - planners and workers are picked from the task, within the limits"
            if limits.dynamic
            else " - one worker per plan step, three planners"
        ),
        f"running at once: {limits.max_parallel}{src('max_parallel')} - the model's own task calls; "
        f"a fan-out runs min({limits.fanout_parallel} [autonomy.fanout.max_parallel], {limits.max_parallel}) = "
        f"{limits.fanout_width}",
        f"sub-agents of a sub-agent: {limits.max_nested}; task calls per turn: {limits.max_spawns or 'no limit'} "
        "(config: autonomy.agents)",
        f"live now: {live}",
    ]
    return "Sub-agent limits:\n" + "\n".join(f"  {line}" for line in lines)


class MaxAgentsCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="maxagents",
            help="Show or set the sub-agent limits: /maxagents [<N>|auto|parallel <N>|dynamic on|off|reset] [--save]",
            aliases=["max-agents"],
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        save = pop_flag(args, "--save")
        pop_flag(args, "--session")  # the default; accepted so the habit from /autocompact works
        if any(a.startswith("--") for a in args):
            return reply(USAGE)
        live = ctx.sessions.get(session_id) if session_id else None
        try:
            changes = parse(args)
        except AgentLimitsError as e:
            return reply(f"{e}\n{USAGE}")
        if changes is None:  # reset
            if live is None:
                return reply("No active session: reset drops a session's own settings.")
            live.stored.meta.pop("agent_limits", None)
            ctx.store.save(live.stored)
            return self._answer(ctx, live, "Back to the config's limits for this session.")
        if not changes:
            return self._answer(ctx, live, "")
        if save:
            try:
                path, backup = write_user_config(changes)
            except confio.ConfigError as e:
                return reply(f"Invalid config: {e}")
            ctx.apply_file_config(live.perms.cwd if live is not None else None)
            lead = f"Saved in {path}" + (f" (backup: {backup.name})." if backup else ".")
            if live is not None and (own := clean_override(live.stored.meta.get("agent_limits"))):
                for key in changes:  # a setting saved for good replaces the session's own, or it would hide it
                    own.pop(key, None)
                live.stored.meta["agent_limits"] = own
                ctx.store.save(live.stored)
            now = agent_limits(ctx.config)
            effective = {
                "max_agents": now.describe_max_agents() if now.auto else now.max_agents,
                "max_parallel": now.max_parallel,
                "dynamic": now.dynamic,
            }
            if any(str(effective[key]) != str(value) for key, value in changes.items()):
                overlay = project_config_path(default_project_dir())
                lead += (
                    f" A project config ({overlay}) with an `ultracode:` or `autonomy:` section replaces that "
                    "whole section: edit it there as well."
                )
        else:
            if live is None:
                return reply("No active session: use --save to keep it in your config.")
            own = clean_override(live.stored.meta.get("agent_limits"))
            own.update(changes)
            live.stored.meta["agent_limits"] = own
            ctx.store.save(live.stored)
            lead = "For this session only (add --save to keep it)."
        return self._answer(ctx, live, lead)

    @staticmethod
    def _limits(ctx: Any, live: Any) -> AgentLimits:
        return session_limits(ctx.config, live) if live is not None else agent_limits(ctx.config)

    def _answer(self, ctx: Any, live: Any, lead: str) -> dict[str, Any]:
        limits = self._limits(ctx, live)
        running = ctx.subagents.live(live.session_id) if live is not None else 0
        if live is not None:
            live.emit("session.info", live.live_info())
        text = describe(limits, running)
        if sections := project_sections():
            text += (
                f"\n  note: a trusted project config has its own {' and '.join(f'`{k}:`' for k in sections)} "
                "section, which replaces the whole section of yours for this gateway"
            )
        applies = (
            "The model's task calls follow this at once; /ultracode and fan-out use it from their next run."
            if lead
            else ""
        )
        return reply("\n".join(p for p in (lead, text, applies) if p), agent_limits=payload(limits, running))
