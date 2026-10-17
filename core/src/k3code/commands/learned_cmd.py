"""/learned [forget <id> | purge [--all] --yes]: see, drop and clear what learning stored for this project and this
machine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import pop_flag, reply, session_cwd, split_args
from k3code.learning import distiller, gotchas, optimizer
from k3code.learning.decisions import DecisionLog, project_id
from k3code.memory import learned_notes_path, user_memory_path
from k3code.paths import home

USAGE = "/learned | /learned forget <id> | /learned purge [--all] --yes"
#: Exactly what each purge removes and keeps; the help, the confirmation prompt and the README say the same.
PURGE = (
    "/learned purge removes this project's auto gotchas (gotchas.auto.md), learned notes (learned.md) and decision "
    "log rows (learning/decisions.db), and every overlay not in an active experiment"
)
PURGE_ALL = (
    "/learned purge --all also removes every project's decision log rows and the learned preferences "
    "(learning/preferences.json and the auto section of USER.md)"
)
PURGE_KEEPS = "Both keep what you confirmed: accepted gotchas (gotchas.md, machine gotchas) and your own USER.md lines"
TEXT_MAX = 100
_LINE_KINDS = ("gotcha", "auto gotcha", "note", "machine gotcha")


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    path: Path
    text: str
    line: str  # the exact stored line (files) or the preference text

    def row(self) -> str:
        shown = self.text if len(self.text) <= TEXT_MAX else self.text[: TEXT_MAX - 1] + "…"
        return f"{self.id}  {self.kind:<14} {shown}  [{self.path}]"


def _id(kind: str, path: Path, line: str) -> str:
    return hashlib.sha1(f"{kind}{path}{line}".encode()).hexdigest()[:8]


def _file_items(kind: str, path: Path) -> list[Item]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        if ln.startswith("- "):
            out.append(Item(_id(kind, path, ln), kind, path, ln[2:].strip(), ln))
    return out


def _overlay_id(path: Path) -> str:
    return path.name.split("-", 1)[0]


def collect(cwd: Path) -> list[Item]:
    h = home()
    pid = project_id(cwd)
    items = _file_items("gotcha", gotchas.gotchas_path(pid, h))
    items += _file_items("auto gotcha", gotchas.auto_gotchas_path(pid, h))
    items += _file_items("note", learned_notes_path(cwd))
    items += _file_items("machine gotcha", gotchas.user_gotchas_path(h))
    pref_path = h / "learning" / "preferences.json"
    for p in distiller.load_preferences(h, top=1_000_000):
        items.append(Item(_id("preference", pref_path, p.text), "preference", pref_path, p.text, p.text))
    for f in sorted((h / "overlays").glob("*.md")):
        try:
            text = " ".join(f.read_text(encoding="utf-8", errors="replace").split())
        except OSError:
            continue
        items.append(Item(_id("overlay", f, f.name), "overlay", f, text or f.name, f.name))
    return items


def _experiments(ctx: Any) -> optimizer.Experiments:
    hub = getattr(ctx, "learning", None)
    return getattr(hub, "experiments", None) or optimizer.Experiments(home())


def _active_overlay_ids(exps: optimizer.Experiments) -> set[str]:
    return {x["id"] for x in exps.active()}


def _forget(item: Item, exps: optimizer.Experiments) -> str:
    if item.kind in _LINE_KINDS:
        lines = item.path.read_text(encoding="utf-8", errors="replace").splitlines()
        kept = [ln for ln in lines if ln != item.line]
        item.path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return f"Forgot {item.id} ({item.kind})."
    if item.kind == "preference":
        h = home()
        every = distiller.load_preferences(h, top=1_000_000)
        rest = [p for p in every if p.text != item.line]
        distiller.write_preferences_json(h, rest)
        distiller.write_auto_section(user_memory_path(), rest, known=[p.text for p in every])
        return (
            f"Forgot {item.id} (preference). The next daily distill may learn it again "
            "if your decisions still show the pattern."
        )
    xid = _overlay_id(item.path)
    if xid in _active_overlay_ids(exps) and exps.rollback(xid):
        return f"Forgot {item.id} (overlay): rolled back experiment {xid}."
    item.path.unlink(missing_ok=True)
    return f"Forgot {item.id} (overlay file deleted)."


def _purge_decisions(ctx: Any, cwd: Path, *, everything: bool) -> int:
    """Delete this project's decision log rows (``everything``: every row); returns how many. Uses the gateway's open
    log; a context without a learning hub opens the file itself, and creates none that is missing."""
    log = getattr(getattr(ctx, "learning", None), "log", None)
    own = log is None
    if own:
        if not (home() / "learning" / "decisions.db").is_file():
            return 0
        log = DecisionLog(home())
    try:
        return log.delete_all() if everything else log.delete_project(cwd)
    finally:
        if own:
            log.close()


def _purge(ctx: Any, cwd: Path, *, everything: bool) -> str:
    """Remove what PURGE (and with ``everything``, PURGE_ALL) names, keep what PURGE_KEEPS names; returns the report.
    The decision log goes first: should it fail (a locked database), nothing else is gone yet and a retry is whole."""
    rows = _purge_decisions(ctx, cwd, everything=everything)
    active = _active_overlay_ids(_experiments(ctx))
    h = home()
    gone = 0
    for p in (gotchas.auto_gotchas_path(project_id(cwd), h), learned_notes_path(cwd)):
        if p.is_file():
            p.unlink()
            gone += 1
    for f in (h / "overlays").glob("*.md"):
        if _overlay_id(f) not in active:
            f.unlink()
            gone += 1
    done = [f"{gone} file(s)", f"{rows} decision log row(s)" + ("" if everything else " of this project")]
    if everything:
        prefs = [p.text for p in distiller.load_preferences(h, top=1_000_000)]
        user_md = user_memory_path()
        text = user_md.read_text(encoding="utf-8") if user_md.is_file() else ""
        if any(ln.strip() == distiller.HEADING for ln in text.splitlines()):  # no block, nothing to clear or create
            # only the managed block is rewritten; ``known`` (what the distiller wrote, as in ``distill``) tells an
            # old unclosed block's lines from the user's, which stay
            distiller.write_auto_section(user_md, [], known=prefs)
        (h / "learning" / "preferences.json").unlink(missing_ok=True)
        done.append(f"{len(prefs)} learned preference(s)")
    return "Purged " + ", ".join(done) + "."


class LearnedCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="learned",
            help=f"List, forget or purge what k3code learned: {USAGE}\n{PURGE}.\n{PURGE_ALL}.\n{PURGE_KEEPS}.",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        yes = pop_flag(args, "--yes") or pop_flag(args, "-y")
        everything = pop_flag(args, "--all")
        cwd = session_cwd(ctx, session_id)
        sub = args[0] if args else "list"
        if everything and sub != "purge":
            return reply(f"--all goes with purge only. Usage: {USAGE}")
        if sub == "list" and len(args) <= 1:
            items = collect(cwd)
            if not items:
                return reply("Nothing learned yet for this project or machine.")
            return reply("\n".join(["Learned (forget one: /learned forget <id>):", *(i.row() for i in items)]))
        if sub == "forget" and len(args) == 2:
            item = next((i for i in collect(cwd) if i.id == args[1]), None)
            if item is None:
                return reply(f"No learned item {args[1]}. See /learned.")
            return reply(_forget(item, _experiments(ctx)))
        if sub == "purge" and len(args) == 1:
            if not yes:
                what = f"{PURGE}. {PURGE_ALL}." if everything else f"{PURGE}."
                again = "/learned purge --all --yes" if everything else "/learned purge --yes"
                return reply(f"{what} {PURGE_KEEPS}. Run {again} to confirm.")
            return reply(_purge(ctx, cwd, everything=everything))
        return reply(f"Usage: {USAGE}")


__all__ = ["LearnedCommand"]
