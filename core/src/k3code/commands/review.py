"""/review [staged|unstaged|<base>..HEAD|<path>]: model-driven review of a diff or file as a sub-turn."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from k3code.commands import CommandDef
from k3code.commands._util import reply, session_cwd, split_args
from k3code.goals import _extract_json_object

RUBRIC_PATH = Path(__file__).resolve().parent.parent / "prompts" / "review_rubric.md"
MAX_INPUT_CHARS = 120_000
_SEVERITIES = ("P0", "P1", "P2", "P3")


async def _git(cwd: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def collect_target(cwd: Path, target: str) -> tuple[str, str]:
    """``(description, text)`` to review; raises ValueError with a user-facing message."""
    rc, out = await _git(cwd, "rev-parse", "--is-inside-work-tree")
    in_repo = rc == 0 and out.strip() == "true"
    path = (cwd / target).expanduser() if target else None
    if target in ("", "all"):
        if not in_repo:
            raise ValueError("Not a git repository; give /review a file or directory path.")
        rc, staged = await _git(cwd, "diff", "--cached")
        rc, unstaged = await _git(cwd, "diff")
        text = ""
        if staged.strip():
            text += "# Staged changes\n" + staged
        if unstaged.strip():
            text += "\n# Unstaged changes\n" + unstaged
        if not text.strip():
            raise ValueError("Nothing to review: no staged or unstaged changes.")
        return "staged + unstaged changes", text
    if target == "staged":
        _, text = await _git(cwd, "diff", "--cached")
        desc = "staged changes"
    elif target == "unstaged":
        _, text = await _git(cwd, "diff")
        desc = "unstaged changes"
    elif ".." in target and not (path and path.exists()):
        if not in_repo:
            raise ValueError("Not a git repository.")
        rc, text = await _git(cwd, "diff", target)
        if rc != 0:
            raise ValueError(f"git diff {target} failed: {text.strip()[:200]}")
        desc = f"diff {target}"
    elif path is not None and path.is_file():
        body = path.read_text(encoding="utf-8", errors="replace")
        numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(body.splitlines(), 1))
        return f"file {target}", f"# File {target} (line-numbered)\n{numbered}"
    elif path is not None and path.is_dir():
        if not in_repo:
            raise ValueError("Directory review needs a git repository (reviews its changes against HEAD).")
        _, text = await _git(cwd, "diff", "HEAD", "--", str(path))
        desc = f"changes under {target}"
    else:
        raise ValueError(f"Not a review target: {target!r} (staged | unstaged | <base>..HEAD | <path>)")
    if not text.strip():
        raise ValueError(f"Nothing to review: no {desc}.")
    return desc, text


def parse_findings(raw: str) -> dict[str, Any]:
    data = _extract_json_object(raw) or _loose_json(raw)
    if data is None:
        raise ValueError(f"reviewer did not return JSON: {raw[:200]!r}")
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "P3").upper()
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        findings.append(
            {
                "severity": sev if sev in _SEVERITIES else "P3",
                "file": str(f.get("file") or ""),
                "line": line,
                "issue": str(f.get("issue") or "").strip(),
                "suggestion": str(f.get("suggestion") or "").strip(),
            }
        )
    findings.sort(key=lambda f: (f["severity"], f["file"], f["line"]))
    return {
        "findings": findings,
        "overall": str(data.get("overall") or ""),
        "summary": str(data.get("summary") or ""),
    }


def _loose_json(raw: str) -> dict[str, Any] | None:
    """The review object is nested, so the flat ``{...}`` fallback in goals fails; try first-to-last brace."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def render_findings(result: dict[str, Any], desc: str) -> str:
    fs = result["findings"]
    lines = [f"Review of {desc}: {len(fs)} finding(s)" + (f" — {result['overall']}" if result["overall"] else "")]
    for f in fs:
        loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        lines.append(f"\n[{f['severity']}] {loc}\n  issue: {f['issue']}\n  suggestion: {f['suggestion']}")
    if result["summary"]:
        lines.append(f"\n{result['summary']}")
    return "\n".join(lines)


class ReviewCommand(CommandDef):
    def __init__(self) -> None:
        super().__init__(
            name="review",
            help="Review changes with the main model: /review [staged|unstaged|<base>..HEAD|<path>]",
        )

    async def handle(self, ctx: Any, session_id: str | None, arg: str) -> dict[str, Any]:
        args = split_args(arg)
        cwd = session_cwd(ctx, session_id)
        try:
            desc, text = await collect_target(cwd, args[0] if args else "")
        except ValueError as e:
            return reply(str(e))
        truncated = len(text) > MAX_INPUT_CHARS
        if truncated:
            text = text[:MAX_INPUT_CHARS] + "\n…(truncated)"
        rubric = RUBRIC_PATH.read_text(encoding="utf-8")
        live = ctx.sessions.get(session_id) if session_id else None
        model = live.stored.model if live and live.stored.model else None
        user = f"Review the following {desc}.\n\n{text}"
        raw = await ctx.oneshot(rubric, user, model_key=model, max_tokens=4096)
        try:
            result = parse_findings(raw)
        except ValueError as e:
            return reply(f"Review failed: {e}")
        out = render_findings(result, desc) + ("\n(input truncated)" if truncated else "")
        return reply(out, review=result)
