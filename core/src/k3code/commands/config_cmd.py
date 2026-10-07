"""/config get|set|edit|path|rollback [--project]."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import yaml

from k3code import confio
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, session_cwd, split_args
from k3code.paths import project_config_path, user_config_path
from k3code.redact import redact

USAGE = "Usage: /config get [key] | set <key> <value> [--project] | edit [--project] | path | rollback [--project]"


def target_path(cwd: Path, project: bool) -> Path:
    return project_config_path(cwd) if project else user_config_path()


def config_set(path: Path, key: str, raw: str) -> Path | None:
    """Validate-then-write ``key=value`` into ``path``; returns the backup path (None if no prior file)."""
    data = confio.read_yaml(path)
    confio.set_path(data, key, confio.parse_value(raw))
    confio.validate(data)
    return confio.write_yaml(path, data)


def config_rollback(path: Path) -> Path:
    bak = confio.latest_backup(path)
    if bak is None:
        raise confio.ConfigError(f"no backup of {path.name} to roll back to")
    # Rolling back is itself reversible: the current file is backed up first.
    data = confio.read_yaml(bak)
    confio.write_yaml(path, data)
    bak.unlink()
    return bak


def open_editor(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return subprocess.call([*shlex.split(editor), str(path)])


class ConfigCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="config", help=USAGE)

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        project = pop_flag(args, "--project")
        cwd = session_cwd(ctx, session_id)
        path = target_path(cwd, project)
        sub = args[0] if args else "get"
        rest = args[1:]
        if sub == "path":
            return reply(f"user: {user_config_path()}\nproject: {project_config_path(cwd)}")
        if sub == "get":
            effective = redact(ctx.config.model_dump())
            effective.get("providers") and [p.pop("api_key", None) for p in effective["providers"]]
            if not rest:
                return reply(yaml.safe_dump(effective, sort_keys=False).strip(), config=effective)
            try:
                value = confio.get_path(effective, rest[0])
            except KeyError:
                return reply(f"No such config key: {rest[0]}")
            text = yaml.safe_dump(value, sort_keys=False).strip() if isinstance(value, dict | list) else str(value)
            return reply(f"{rest[0]} = {text}", key=rest[0], value=value)
        if sub == "set":
            if len(rest) < 2:
                return reply("Usage: /config set <key> <value> [--project]")
            try:
                bak = config_set(path, rest[0], " ".join(rest[1:]))
            except confio.ConfigError as e:
                return reply(f"Invalid config: {e}")
            ctx.apply_file_config(cwd)
            return reply(f"Set {rest[0]} in {path}" + (f" (backup: {bak.name})" if bak else ""))
        if sub == "edit":
            return reply(f"Edit {path} (run `$EDITOR {path}` or `k3code config edit`).", path=str(path))
        if sub == "rollback":
            try:
                bak = config_rollback(path)
            except confio.ConfigError as e:
                return reply(str(e))
            ctx.apply_file_config(cwd)
            return reply(f"Rolled {path} back to {bak.name}.")
        return reply(USAGE)
