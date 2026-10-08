"""/output-style [name] [--default]: pick the style appended to the system prompt."""

from __future__ import annotations

from typing import Any

from k3code import outputstyle
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, session_cwd, split_args
from k3code.prompting import effective_style


class OutputStyleCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="output-style",
            aliases=["style"],
            help="Show or set the output style: /output-style [default|concise|explanatory|learning|<custom>] "
            "[--default]",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        make_default = pop_flag(args, "--default")
        cwd = session_cwd(ctx, session_id)
        live = ctx.sessions.get(session_id) if session_id else None
        meta = live.stored.meta if live else None
        if not args:
            cur = effective_style(ctx.config, meta)
            listing = ", ".join(f"{n}{' *' if n == cur else ''}" for n in outputstyle.available(cwd))
            return reply(f"Output style: {cur}\nAvailable: {listing}", style=cur)
        name = args[0]
        if outputstyle.style_text(name, cwd) is None:
            return reply(f"Unknown output style: {name}. Available: {', '.join(outputstyle.available(cwd))}")
        if live is not None:
            live.stored.meta["output_style"] = name
            ctx.store.save(live.stored)
        if make_default or live is None:
            from k3code.commands.config_cmd import config_set
            from k3code.paths import user_config_path

            config_set(user_config_path(), "output_style", name)
            ctx.apply_file_config(cwd)
        where = "this session" + (" and the default" if make_default and live else "")
        return reply(f"Output style set to {name} for {where} (applies from the next turn).", style=name)
