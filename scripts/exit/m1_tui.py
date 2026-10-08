#!/usr/bin/env python3
"""M1 exit checks: scripted TUI tapes (pexpect + pyte on the real `node tui/dist/entry.js`) against the gateway on
the fake provider, plus the export/import round trip and the live /review check.

Each flow gets its own temp K3CODE_HOME + cwd (fake-provider scripts are per flow). Assertions are on the rendered
screen text. Usage: m1_tui.py [flow ...]   (no args = all). Re-execs under `uv run --with pexpect --with pyte`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import pexpect  # noqa: F401
    import pyte  # noqa: F401
except ImportError:
    os.execvp("uv", ["uv", "run", "--with", "pexpect", "--with", "pyte", "python", __file__, *sys.argv[1:]])

from lib import emit, live_backend, live_providers_yaml, run  # noqa: E402
from tui_harness import Tui, core_python, write_home  # noqa: E402

M = "M1"
TMP = Path(tempfile.mkdtemp(prefix="exit-m1-"))
FLOWS: dict[str, tuple[str, str, object]] = {}


def flow(key: str, criterion: str, how: str):
    def deco(fn):
        FLOWS[key] = (criterion, how, fn)
        return fn

    return deco


class Fail(Exception):
    pass


def need(cond: object, msg: str) -> None:
    if not cond:
        raise Fail(msg)


def mk(name: str, script: list[dict] | None, git: bool = False, **kw) -> tuple[Tui, Path, Path]:
    home, cwd = TMP / f"{name}-home", TMP / f"{name}-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    if git:
        run(
            "git init -q -b main . && git config user.email t@t && git config user.name t && echo a > a.txt "
            "&& git add . && git commit -qm init",
            cwd=cwd,
        )
    write_home(home, script, **kw)
    t = Tui(home, cwd)
    need(t.boot(), "TUI did not boot")
    t.pump(1.5)
    return t, home, cwd


def badge_on(t: Tui) -> bool:
    return bool(re.search(r"◉ focus", t.text()))  # case-sensitive: the status-line badge, not echoed text


def wait_badge(t: Tui, want: bool, timeout: float = 10) -> bool:
    import time

    end = time.time() + timeout
    while time.time() < end:
        t.pump(0.4)
        if badge_on(t) == want:
            return True
    return False


def tail_screen(t: Tui, n: int = 14) -> str:
    return "\n".join(ln for ln in t.text().splitlines() if ln.strip())[-1800:] if n else ""


TOOL_DONE = {"type": "text", "text": "turn-complete", "when": "turn_after_tool"}


def bash_step(cmd: str) -> dict:
    return {"type": "tool_call", "id": "b1", "name": "bash", "arguments": {"command": cmd}, "when": "turn_first"}


@flow("plan_refuse", "Plan mode (Shift+Tab): a write is refused", "TUI tape: Shift+Tab x2 -> plan, model calls write")
def plan_refuse() -> str:
    t, home, cwd = mk(
        "planr",
        [
            {
                "type": "tool_call",
                "id": "w1",
                "name": "write",
                "arguments": {"path": "a.txt", "content": "x"},
                "when": "turn_first",
            },
            TOOL_DONE,
        ],
    )
    try:
        t.key("shift_tab")
        t.key("shift_tab")
        need(t.wait(r"mode: plan", 10), "mode did not reach plan\n" + tail_screen(t))
        t.line("please write a.txt")
        need(t.wait(r"read-only", 20), "no refusal on screen\n" + tail_screen(t))
        need(not (cwd / "a.txt").exists(), "a.txt was written in plan mode")
        return "status bar 'plan'; screen: Plan mode is read-only; present the plan with exit_plan; a.txt not created"
    finally:
        t.close()


@flow(
    "plan_approve", "Plan mode: exit_plan approval dialog switches mode", "TUI tape: exit_plan -> pick 'Approve plan'"
)
def plan_approve() -> str:
    t, home, cwd = mk(
        "plana",
        [
            {
                "type": "tool_call",
                "id": "p1",
                "name": "exit_plan",
                "arguments": {"plan": "1. write a.txt\n2. verify with cat"},
                "when": "turn_first",
            },
            TOOL_DONE,
        ],
    )
    try:
        t.key("shift_tab")
        t.key("shift_tab")
        need(t.wait(r"mode: plan", 10), "mode did not reach plan")
        t.line("plan it")
        need(t.wait(r"Approve this plan\?", 20), "no plan approval dialog\n" + tail_screen(t))
        need(t.wait(r"Keep planning", 5), "dialog lacks choices")
        t.key("enter")
        need(t.wait(r"Plan approved; mode is now default", 20), "plan not approved\n" + tail_screen(t))
        need(
            t.wait(r"│ default │ default │", 10, ever=False) or "default" in t.text().splitlines()[-2],
            "mode not default",
        )
        return (
            "dialog 'Approve this plan?' with 3 choices -> Enter -> 'Plan approved; mode is now default. Implement it.'"
        )
    finally:
        t.close()


@flow(
    "perm",
    "Permissions: ask / allow once / deny / always allow (rule persists)",
    "TUI tape: same bash call 4 turns: once, deny, always, then no prompt",
)
def perm() -> str:
    t, home, cwd = mk("perm", [bash_step("touch ran.txt"), TOOL_DONE])
    ran = cwd / "ran.txt"
    try:
        t.line("run it")
        need(t.wait(r"approval required", 20), "no ask dialog\n" + tail_screen(t))
        t.key("enter")  # Allow once
        need(t.wait(r"turn-complete", 20), "turn 1 did not finish")
        need(ran.exists(), "allow once: not run")
        ran.unlink()
        t.wait_gone(r"approval required", 5)
        t.mark()
        t.line("run it again")
        need(t.wait(r"approval required", 20), "once did not re-ask")
        t.child.send("4")
        t.pump(1)  # Deny
        need(t.wait(r"turn-complete", 20), "turn 2 did not finish")
        need(not ran.exists(), "deny: command ran")
        t.mark()
        t.line("run it a third time")
        need(t.wait(r"approval required", 20), "deny did not re-ask")
        t.child.send("3")
        t.pump(1)  # Always allow
        need(t.wait(r"turn-complete", 20), "turn 3 did not finish")
        need(ran.exists(), "always: not run")
        ran.unlink()
        t.mark()
        t.line("and a fourth")
        t.pump(5)
        need("approval required" not in t.ever_text(), "prompted again after 'Always allow'")
        need(ran.exists(), "turn 4 did not run without a prompt")
        cfg = cwd / ".k3code" / "config.yaml"
        need(cfg.exists() and "touch" in cfg.read_text(), "always-rule not persisted in project config")
        return "once ran+re-asked; deny blocked; always ran and turn 4 had no prompt; rule in .k3code/config.yaml"
    finally:
        t.close()


@flow(
    "cmds",
    "/compact /model /effort /rename /fork /branch",
    "TUI tape on a temp git repo; asserts command output on screen",
)
def cmds() -> str:
    t, home, cwd = mk(
        "cmds",
        [{"type": "text", "text": "reply-one"}, {"type": "usage", "prompt_tokens": 5, "completion_tokens": 2}],
        git=True,
    )
    ev = []
    try:
        for i in range(4):
            t.line(f"question {i}")
            need(t.wait(r"reply-one", 15), "no reply")
            t.wait(r"ready", 10)
        for cmd, pat in [
            ("/compact", r"Compacted \d+ messages"),
            ("/model cheap", r"model → cheap"),
            ("/effort high", r"Reasoning effort set to: high"),
            ("/rename mytitle", r"Renamed to: mytitle"),
            ("/fork", r"Forked → \w+"),
            ("/branch feat-x", r"Created branch feat-x"),
        ]:
            t.mark()
            t.line(cmd)
            need(t.wait(pat, 20, ever=True), f"{cmd}: expected /{pat}/\n" + tail_screen(t))
            m = re.search(pat, t.ever_text())
            ev.append(f"{cmd} -> {m.group(0) if m else '?'}")
            t.pump(1)
        rc, out = run("git branch", cwd=cwd)
        need("feat-x" in out, f"git branch feat-x missing: {out}")
        return "; ".join(ev) + "; git branch lists feat-x"
    finally:
        t.close()


@flow(
    "resume_export",
    "/resume + export -> import round trip into a second home",
    "TUI creates a session; `k3code export --all` from home A; `k3code import --yes` into home B; /resume in TUI on B",
)
def resume_export() -> str:
    t, home, cwd = mk("rexp", [{"type": "text", "text": "stored-reply"}])
    try:
        t.line("remember this")
        need(t.wait(r"stored-reply", 15), "no reply")
        t.line("/rename round-trip")
        t.wait(r"Renamed to", 10)
        t.pump(1)
        m = re.search(r"Session: ([0-9a-f]{16})", t.ever_text())
        need(m, "session id not on screen")
        sid = m.group(1)
    finally:
        t.close()
    t2 = None
    py = core_python()
    env = dict(os.environ, K3CODE_HOME=str(home))
    bundle = TMP / "rt.k3bundle"
    rc, out = run([py, "-m", "k3code.cli", "export", "--all", str(bundle)], env=env, cwd=cwd, timeout=60)
    need(rc == 0 and bundle.exists(), f"export failed: {out}")
    home_b = TMP / "rexp-b-home"
    write_home(home_b, [{"type": "text", "text": "x"}])
    envb = dict(os.environ, K3CODE_HOME=str(home_b))
    rc, out2 = run([py, "-m", "k3code.cli", "import", str(bundle), "--yes"], env=envb, cwd=cwd, timeout=60)
    need(rc == 0 and sid in out2, f"import failed or session missing: {out2}")
    # resume the imported session inside a TUI on the second home
    cwd_b = TMP / "rexp-b-cwd"
    cwd_b.mkdir(exist_ok=True)
    t2 = Tui(home_b, cwd_b)
    try:
        need(t2.boot(), "TUI B did not boot")
        t2.pump(1.5)
        t2.line(f"/resume {sid}")
        need(t2.wait(rf"Session: {sid}", 20), "resume in B failed\n" + tail_screen(t2))
        need(
            t2.wait(r"remember this", 10) and "stored-reply" in t2.text(),
            "resumed transcript not shown\n" + tail_screen(t2),
        )
        return (
            f"export ok ({bundle.stat().st_size} B); import: {out2.strip().splitlines()[-1]}; "
            f"/resume {sid} in home B shows banner + transcript"
        )
    finally:
        t2.close()


@flow("goal", "/goal reaches done", "TUI tape: judge on the cheap model returns {verdict: done}")
def goal() -> str:
    t, home, cwd = mk(
        "goal",
        [
            {"type": "text", "text": "working on the goal now", "model": "m"},
            {"type": "text", "text": '{"verdict": "done", "reason": "objective met"}', "model": "j"},
        ],
        cheap="j",
    )
    try:
        t.line("/goal write a haiku")
        need(t.wait(r"working on the goal now", 25), "goal turn did not run\n" + tail_screen(t))
        need(t.wait(r"done|achieved|objective met", 25, ever=True), "no 'done' shown\n" + tail_screen(t))
        t.mark()
        t.line("/goal status")
        t.wait(r"goal", 10, ever=True)
        need(re.search(r"done", t.ever_text(), re.I), "/goal status does not say done\n" + t.ever_text()[-600:])
        return "turn ran, judge verdict done; /goal status: " + (
            re.search(r".*done.*", t.ever_text(), re.I).group(0).strip()
        )
    finally:
        t.close()


@flow(
    "bg_strip", "Agent strip: /bg session goes working -> completed", "TUI tape: /bg with a 6 s bash; sample the strip"
)
def bg_strip() -> str:
    t, home, cwd = mk(
        "bg",
        [
            {"type": "tool_call", "id": "b", "name": "bash", "arguments": {"command": "sleep 6"}, "when": "turn_first"},
            {"type": "text", "text": "bg-finished", "when": "turn_after_tool"},
        ],
        permission_mode="yolo",
    )
    try:
        t.line("/bg do the long thing")
        need(t.wait(r"working · do the long thing", 15, ever=True), "strip never showed working\n" + tail_screen(t))
        need(t.wait(r"completed · do the long thing", 30, ever=True), "strip never showed completed\n" + tail_screen(t))
        return "strip rows seen in order: '◐ do the long thing … · working' then '✓ … · completed'"
    finally:
        t.close()


@flow(
    "needs_input",
    "A needs-input approval from a /bg session is answered inline",
    "TUI tape: bg session asks for bash approval; strip shows 'needs input'; Enter on the row attaches; answer",
)
def needs_input() -> str:
    t, home, cwd = mk("ni", [bash_step("touch ni.txt"), TOOL_DONE])
    try:
        t.line("/bg touch the file")
        need(
            t.wait(r"needs input · touch the file", 25, ever=True), "strip never showed needs input\n" + tail_screen(t)
        )
        t.key("down")
        t.pump(0.5)
        t.key("enter")
        need(t.wait(r"approval required", 20), "approval dialog did not appear on attach\n" + tail_screen(t))
        t.key("enter")
        need(t.wait(r"turn-complete|ready", 20), "bg turn did not finish after answering")
        t.pump(2)
        need((cwd / "ni.txt").exists(), "approved command did not run")
        return (
            "strip: '● … · needs input'; attached with Enter; "
            "dialog 'approval required' answered 'Allow once'; ni.txt created"
        )
    finally:
        t.close()


@flow("focus", "Focus mode hides tool output", "TUI tape: /focus on -> tool block absent; /focus off -> present")
def focus() -> str:
    t, home, cwd = mk("focus", [bash_step("echo TOOLOUT-MARKER"), TOOL_DONE], permission_mode="yolo")
    try:
        t.line("/focus on")
        need(wait_badge(t, True), "FOCUS badge not shown\n" + tail_screen(t))
        t.mark()
        t.line("run the tool")
        need(t.wait(r"turn-complete", 20), "no final answer")
        t.pump(1)
        need(
            "TOOLOUT-MARKER" not in t.text() and "Tool calls" not in t.text(),
            "tool output visible in focus mode\n" + tail_screen(t),
        )
        t.line("/focus off")
        need(t.wait(r"focus view disabled", 10, ever=True), "focus not disabled")
        need(wait_badge(t, False), "FOCUS badge still shown")
        t.mark()
        t.line("run the tool again")
        need(t.wait(r"turn-complete", 20), "no final answer 2")
        need(t.wait(r"Tool calls", 5), "tool block missing with focus off\n" + tail_screen(t))
        return (
            "focus on: FOCUS badge, final answer shown, no 'Tool calls'/tool output; "
            "focus off: 'Tool calls' block visible"
        )
    finally:
        t.close()


def review_live() -> None:
    crit = "/review finds a seeded off-by-one bug (live model)"
    backend = live_backend()
    how = f"git repo with an unstaged diff seeding `range(1, len(xs))`; TUI `/review`; {backend['label']}"
    if not backend["ok"]:
        emit(
            M,
            crit,
            how,
            "PENDING",
            f"live model unavailable: {backend['detail']}",
            "Log in to Claude Code (`claude`), or set K3_ALLOW_OMNIROUTE=1 with a working key; "
            "then re-run `scripts/exit/m1_tui.py review`",
        )
        return
    cwd, home = TMP / "rev-cwd", TMP / "rev-home"
    cwd.mkdir(parents=True)
    (cwd / "total.py").write_text(
        "def total(xs):\n    s = 0\n    for i in range(len(xs)):\n        s += xs[i]\n    return s\n"
    )
    run(
        "git init -q -b main . && git config user.email t@t && git config user.name t "
        "&& git add . && git commit -qm init",
        cwd=cwd,
    )
    (cwd / "total.py").write_text(
        "def total(xs):\n    s = 0\n    for i in range(1, len(xs)):\n        s += xs[i]\n    return s\n"
    )
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("permission_mode: default\n" + live_providers_yaml(backend))
    t = Tui(home, cwd, script=False)
    try:
        t.boot()
        t.pump(1.5)
        t.line("/review")
        t.wait(r"P[0-3]|no (issues|findings)|failed", 120, ever=True)
        t.pump(2)
        txt = t.ever_text()
        hit = re.search(
            r"off.by.one|range\(1|skips?\b.*(first|index 0|element)|first (element|item)"
            r"|index 0|xs\[0\]",
            txt,
            re.I,
        )
        if hit:
            emit(M, crit, how, "PASS", "finding mentions the seeded bug: " + hit.group(0) + "\n" + txt[-500:])
        elif re.search(r"429|quota|rate", txt, re.I):
            emit(
                M,
                crit,
                how,
                "PENDING",
                txt[-400:],
                "Re-run `scripts/exit/m1_tui.py review` after the OmniRoute quota resets",
            )
        else:
            emit(M, crit, how, "FAIL", "seeded bug not reported:\n" + txt[-800:])
    finally:
        t.close()


def main() -> None:
    want = sys.argv[1:] or [*FLOWS, "review"]
    if shutil.which("node") is None or not (HERE.parents[1] / "tui/dist/entry.js").exists():
        for k in want:
            emit(
                M,
                f"TUI flow {k}",
                "pexpect",
                "PENDING",
                "tui/dist/entry.js missing",
                "cd tui && npm ci && npm run build",
            )
        return
    for key in want:
        if key == "review":
            review_live()
            continue
        crit, how, fn = FLOWS[key]
        mem = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout.split("\n")[1].split()
        if int(mem[6]) < 4096:
            emit(M, crit, how, "PENDING", f"only {mem[6]} MB available (<4 GB)", "Re-run when >=4 GB RAM is free")
            continue
        try:
            ev = fn()
            emit(M, crit, how, "PASS", ev)
        except Fail as e:
            head, _, rest = str(e).partition("\n")
            print(f"FAIL {key}: {e}", file=sys.stderr)
            emit(M, crit, how, "FAIL", f"{rest}\nFAILED: {head}")
        except Exception:  # noqa: BLE001
            emit(M, crit, how, "FAIL", traceback.format_exc())


if __name__ == "__main__":
    main()
    shutil.rmtree(TMP, ignore_errors=True)
