"""/update: show current vs latest version + changelog; ``/update now`` applies, ``/update rollback`` reverts."""

from __future__ import annotations

import asyncio
from typing import Any

from k3code import update as upd
from k3code.commands import CommandDef
from k3code.commands._util import reply


async def _git_install(url: str, ref: str, cur: str, apply: bool) -> str:
    """What `k3code update` reports for an `install.sh --from-git` install: the ref's head against the build."""
    try:
        head = await asyncio.wait_for(asyncio.to_thread(upd.remote_head, url, ref), upd.LS_REMOTE_TIMEOUT + 5)
    except TimeoutError:
        return f"current: {cur}\nCould not check {ref} of {url}: timed out"
    except Exception as e:  # noqa: BLE001
        return f"current: {cur}\nCould not check {ref} of {url}: {e}"
    if upd.is_commit_sha(ref):
        return (
            f"current: {cur}\nThis install is pinned to commit {ref[:12]}: there is nothing to update. "
            "Reinstall with `sh install.sh --from-git --ref Main` to follow a branch."
        )
    head_line = f"current: {cur}\nlatest:  {head[:7]} ({ref})"
    have = upd.installed_sha()
    if have and head.startswith(have):
        return f"{head_line}\nAlready up to date."
    if not apply:
        return f"{head_line}\nRun `/update now` to install it (smoke-tested, auto-rollback; the daemon restarts)."
    return await asyncio.to_thread(upd.apply_detached)


class UpdateCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(name="update", help="Check for updates: /update [now|rollback]")

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        sub = arg.strip().lower()
        if sub == "rollback":
            res = await asyncio.to_thread(upd.rollback)
            return reply(res.message)
        cfg = upd.update_settings()
        cur = upd.current_version() or "(not a versioned install)"
        src = upd.source_checkout()
        built_from_source = src is not None and (src / ".git").exists()
        why = ""
        rel = None
        try:
            token = upd.github_token()
            rel = await asyncio.to_thread(upd.fetch_latest, cfg["channel"], cfg["repo"], token)
        except Exception as e:  # noqa: BLE001
            # a git install has no release to download (a private repository answers 404): it follows its ref instead
            if not built_from_source and not (isinstance(e, PermissionError) and upd.git_ref() is not None):
                return reply(f"current: {cur}\nCould not check releases: {e}")
            why = str(e)
        if rel is None and built_from_source:
            # nothing to download (no release yet, or a private repository and no token): the checkout is the source
            if sub != "now":
                return reply(
                    f"current: {cur}\n{why or 'No release has been published yet'}.\n"
                    f"This install is built from {src}. Run `/update now` to pull it and rebuild "
                    "(smoke-tested, auto-rollback; the daemon restarts)."
                )
            return reply(await asyncio.to_thread(upd.apply_detached))
        if rel is None:
            git_ref = upd.git_ref()
            if git_ref is not None:
                return reply(await _git_install(cfg["url"], git_ref, cur, sub == "now"))
            return reply(f"current: {cur}\nNo releases on channel '{cfg['channel']}'.")
        if sub != "now":
            return reply(
                f"current: {cur}\nlatest:  {rel.version} ({cfg['channel']})\n\n{rel.body.strip()[:1500]}\n\n"
                "Run `/update now` to install it (smoke-tested, auto-rollback; the daemon restarts)."
            )

        if not upd.is_newer(rel.version, upd.current_version()):
            return reply(f"current: {cur}\nAlready up to date (latest is {rel.version}).")
        return reply(await asyncio.to_thread(upd.apply_detached))
