"""/config get|set|edit|path|rollback [--project]."""

from __future__ import annotations

import difflib
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from k3code import confio
from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, session_cwd, split_args
from k3code.commands.tune import known_model_keys
from k3code.config import Settings, default_project_dir
from k3code.paths import project_config_path, user_config_path
from k3code.redact import redact

USAGE = "Usage: /config get [key] | set <key> <value> [--project] | edit [--project] | path | rollback [--project]"


def target_path(cwd: Path, project: bool) -> Path:
    return project_config_path(cwd) if project else user_config_path()


#: Top-level keys a config file may carry besides the ``Settings`` fields: read straight from the YAML elsewhere
#: (``sandbox``, ``update``) or written by the setup wizard (``tiers``, ``panes``, the legacy ``searxng``).
EXTRA_FILE_KEYS = frozenset({"sandbox", "update", "tiers", "panes", "searxng"})


def config_set(path: Path, key: str, raw: str, *, known_models: set[str] | None = None) -> Path | None:
    """Validate-then-write ``key=value`` into ``path``; returns the backup path (None if no prior file).

    An unknown top-level key (`permision_mode`) is refused here, though a file that already carries one still loads.
    ``known_models``: the model keys the providers define; ``default_model`` must be one of them (or ``default``).
    """
    top = key.split(".")[0]
    known = set(Settings.model_fields) | EXTRA_FILE_KEYS
    if top not in known:
        near = difflib.get_close_matches(top, sorted(known), n=1)
        raise confio.ConfigError(f"unknown config key {top!r}" + (f" (did you mean {near[0]}?)" if near else ""))
    value = confio.parse_value(raw)
    if key == "providers" and not value:
        raise confio.ConfigError("providers may not be empty: that would remove the whole fallback chain")
    data = confio.read_yaml(path)
    if key == "default_model" and known_models is not None and value != "default" and value not in known_models:
        raise confio.ConfigError(f"default_model {value!r} is not a model key of any provider ({sorted(known_models)})")
    confio.set_path(data, key, value)
    confio.validate(data)
    return confio.write_yaml(path, data)


def config_rollback(path: Path) -> Path:
    """Put the newest backup back byte for byte (its comments stay); the file as it is now is backed up first, so a
    rollback can itself be rolled back."""
    bak = confio.latest_backup(path)
    if bak is None:
        raise confio.ConfigError(f"no backup of {path.name} to roll back to")
    confio.restore_backup(path, bak)
    return bak


def open_editor(path: Path) -> int:
    """Run $VISUAL / $EDITOR on ``path``; a command that cannot start is a ConfigError, not a traceback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    try:
        argv = shlex.split(editor)
    except ValueError as e:
        raise confio.ConfigError(f"$EDITOR is not a valid command ({e}): {editor!r}") from e
    if not argv:
        raise confio.ConfigError("$EDITOR is empty: set $VISUAL or $EDITOR to your editor")
    try:
        return subprocess.call([*argv, str(path)])
    except OSError as e:  # FileNotFoundError, PermissionError
        why = e.strerror or str(e)
        raise confio.ConfigError(f"cannot run the editor {argv[0]!r} ({why}): set $VISUAL or $EDITOR") from e


def edit_config_file(path: Path) -> int:
    """`open_editor` for a config file: what the editor leaves must still be a valid config. If it is not, the previous
    content is put back (the edit is kept beside it as ``<name>.rejected``) and a ConfigError says why. A changed valid
    file gets a backup of the content it had, like every other write."""
    before = path.read_bytes() if path.is_file() else None
    bak: Path | None = None
    if before is not None:
        bak = confio.backup_path(path)
        shutil.copy2(path, bak)
    rc = open_editor(path)
    after = path.read_bytes() if path.is_file() else None
    if after == before or after is None:
        if bak is not None:
            bak.unlink(missing_ok=True)
        return rc
    try:
        confio.validate(confio.read_yaml(path))
    except confio.ConfigError as e:
        if before is None:
            path.unlink()
        else:
            confio.replace_bytes(path, before)
        if bak is not None:
            bak.unlink(missing_ok=True)
        rejected = path.with_name(path.name + ".rejected")
        kept = f"your edit is in {rejected.name}" if _write_new(rejected, after) else "your edit could not be kept"
        raise confio.ConfigError(f"{path.name} is not valid ({e}); kept the previous content, {kept}") from e
    return rc


def _write_new(path: Path, data: bytes) -> bool:
    """Write ``data`` to ``path`` as a new regular file (0600), whatever was there before. Never through a symlink: a
    repository can ship ``.k3code/config.yaml.rejected -> ~/.bashrc``. False when it cannot be written."""
    try:
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return False
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return True


def _learn(ctx: Any, session_id: str | None, kind: str, subject: str, choice: str) -> None:
    hub = getattr(ctx, "learning", None)
    if hub is not None:
        hub.record(kind, ctx.sessions.get(session_id) if session_id else None, subject=subject, choice=choice)


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
                bak = config_set(path, rest[0], " ".join(rest[1:]), known_models=known_model_keys(ctx.config))
            except confio.ConfigError as e:
                return reply(f"Invalid config: {e}")
            ctx.apply_file_config(cwd)
            _learn(ctx, session_id, "config", rest[0], "set")
            note = ""
            if project and cwd.resolve() != default_project_dir().resolve():
                # the running config is shared by all sessions and holds only the gateway project's overlay
                note = (
                    " The permissions: section of this project config applies to sessions in it; other keys apply "
                    "where this directory is the gateway's project (K3CODE_PROJECT_DIR, else its start directory)."
                )
            return reply(f"Set {rest[0]} in {path}" + (f" (backup: {bak.name})" if bak else "") + note)
        if sub == "edit":
            return reply(f"Edit {path} (run `$EDITOR {path}` or `k3code config edit`).", path=str(path))
        if sub == "rollback":
            try:
                bak = config_rollback(path)
            except confio.ConfigError as e:
                return reply(str(e))
            ctx.apply_file_config(cwd)
            _learn(ctx, session_id, "undo", str(path), "config-rollback")
            return reply(f"Rolled {path} back to {bak.name}.")
        return reply(USAGE)
