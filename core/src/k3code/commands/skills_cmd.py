"""/skills [show <name>]."""

from __future__ import annotations

from typing import Any

from k3code import skills as skills_mod
from k3code import trust
from k3code.commands import CommandDef
from k3code.commands._util import reply, session_cwd, split_args


class SkillsCommand(CommandDef):
    headless = True
    needs_server = False

    def __init__(self) -> None:
        super().__init__(name="skills", help="List skills; /skills show <name> prints one in full")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        cwd = session_cwd(ctx, session_id)
        roots = list(ctx.config.skills.roots)
        if args and args[0] == "show":
            if len(args) < 2:
                return reply("Usage: /skills show <name>")
            skill = skills_mod.find_skill(args[1], cwd, roots)
            if skill is None:
                return reply(f"Unknown skill: {args[1]}")
            return reply(skill.text(), name=skill.name, path=str(skill.path))
        found = skills_mod.discover(cwd, roots)
        hint = trust.untrusted_hint(cwd)
        tail = f"\n{hint}" if hint else ""
        if not found:
            return reply("No skills found (looked in $K3CODE_HOME/skills, .k3code/skills and skills.roots)." + tail)
        lines = [f"{s.name} — {s.description[:100]}" for s in found]
        return reply(f"{len(found)} skill(s):\n" + "\n".join(lines) + tail, skills=[s.name for s in found])
