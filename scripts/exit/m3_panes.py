#!/usr/bin/env python3
"""M3 exit checks: k3keys + harness go tests, hint bar, upstream tuios tests, live pane badges (private daemon),
daemon restart, Inbox approval. Hallway test is PENDING (needs a human)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import REPO, emit, run, tail  # noqa: E402

M = "M3"
PANES = REPO / "panes"
CORE = REPO / "core"


def go(args: str, timeout: int = 1500) -> tuple[int, str]:
    return run(f"go test {args}", cwd=PANES, timeout=timeout)


class Daemon:
    """A private tuios daemon: own HOME and XDG dirs, never the user's sessions."""

    def __init__(self, binary: Path, root: Path) -> None:
        self.bin, self.root = binary, root
        for d in ("home", "run", "cfg", "work"):
            (root / d).mkdir(parents=True, exist_ok=True)
        (root / "run").chmod(0o700)
        r = str(root)
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith(("TUIOS", "K3"))},
                    "HOME": f"{r}/home", "XDG_RUNTIME_DIR": f"{r}/run", "XDG_CONFIG_HOME": f"{r}/cfg",
                    "XDG_STATE_HOME": f"{r}/cfg", "XDG_DATA_HOME": f"{r}/cfg"}
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        log = open(self.root / "daemon.log", "a")  # noqa: SIM115
        self.proc = subprocess.Popen([str(self.bin), "daemon"], env=self.env, stdout=log, stderr=log,
                                     start_new_session=True)
        for _ in range(50):
            time.sleep(0.2)
            if self.t("list-sessions")[0] == 0:
                return

    def t(self, *args: str, timeout: int = 60) -> tuple[int, str]:
        return run([str(self.bin), *args], env=self.env, timeout=timeout)

    def agents(self, session: str) -> list[dict]:
        rc, out = self.t("list-agents", "-s", session, "--all", "--json")
        try:
            return json.loads(out).get("agents", [])
        except ValueError:
            return []

    def stop(self) -> None:
        self.t("kill-server")
        if self.proc:
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def demo_cmd(extra_env: dict[str, str], root: Path, session: str, name: str) -> list[str]:
    py = CORE / ".venv" / "bin" / "python"
    args = ["start-agent", "-s", session, "--name", name, "--cwd", str(root / "work"), "--ready-timeout", "5"]
    for k, v in extra_env.items():
        args += ["--env", f"{k}={v}"]
    return args + [f"{py} {CORE / 'scripts' / 'live_panes_demo.py'}"]


def live_checks(tmp: Path) -> None:
    how_live = "docs/k3-panes-test.md live demo: private tuios daemon (own HOME/XDG), core/scripts/live_panes_demo.py"
    rc, out = run("go build -o %s ./cmd/tuios" % (tmp / "tuios"), cwd=PANES, timeout=900)
    if rc != 0:
        for c in ("Pane badges update within 1 s", "After a tuios daemon restart the panes resume",
                  "Inbox approval shows up"):
            emit(M, c, how_live, "FAIL", "go build ./cmd/tuios failed:\n" + out)
        return
    d = Daemon(tmp / "tuios", tmp / "k3")
    d.start()
    try:
        # --- badges: measure report -> visible latency
        ts = tmp / "ts.txt"
        d.t(*demo_cmd({"LIVE_TMP": str(tmp / "k3/work"), "LIVE_TS": str(ts)}, tmp / "k3", "live", "k3pane"))
        seen: dict[str, tuple[float, float]] = {}  # state -> (first poll-seen time, daemon agent_state_at)
        t_end = time.time() + 40
        while time.time() < t_end and "done" not in seen:
            now = time.time()
            for a in d.agents("live"):
                st = a.get("state")
                if st in ("working", "done") and st not in seen:
                    seen[st] = (now, a.get("agent_state_at", 0) / 1e9)
            time.sleep(0.02)
        sent: dict[str, float] = {}
        if ts.exists():
            for line in ts.read_text().splitlines():
                t, st = line.split()
                sent.setdefault(st, float(t))
        lines, worst = [], 0.0
        for st in ("working", "done"):
            if st in seen and st in sent:
                visible = seen[st][0] - sent[st]
                daemon = seen[st][1] - sent[st]
                worst = max(worst, visible)
                lines.append(f"{st}: reported->daemon {daemon * 1000:.0f} ms, reported->visible in list-agents "
                             f"{visible * 1000:.0f} ms")
            else:
                lines.append(f"{st}: never observed (seen={list(seen)}, sent={list(sent)})")
                worst = 99
        emit(M, "Pane badges update within 1 s", how_live + "; latency = gateway state report -> list-agents poll (20 ms)",
             "PASS" if worst < 1.0 else "FAIL", "\n".join(lines) + f"\nworst {worst * 1000:.0f} ms")

        # --- Inbox approval
        d.t(*demo_cmd({"LIVE_TMP": str(tmp / "k3/work2"), "LIVE_APPROVAL": "1"}, tmp / "k3", "appr", "k3appr"))
        (tmp / "k3/work2").mkdir(exist_ok=True)
        out, ok = "", False
        for _ in range(100):
            rc, out = d.t("list-attention", "-s", "appr")
            if "rm -rf" in out or "bash" in out.lower():
                ok = True
                break
            time.sleep(0.3)
        emit(M, "Inbox approval shows up", how_live + " with LIVE_APPROVAL=1; `tuios list-attention -s appr`",
             "PASS" if ok else "FAIL", out)

        # --- daemon restart
        before = {a["window_id"] for s in ("live", "appr") for a in d.agents(s)}
        d.stop()
        time.sleep(1)
        d.start()
        rc, sess = d.t("list-sessions")
        after = {a["window_id"] for s in ("live", "appr") for a in d.agents(s)}
        # the restored layout starts new shells: re-launch the agent in the restored pane and see it report again
        d.t("send-text", "-s", "live", "-w", "k3pane",
            f"LIVE_TMP={tmp / 'k3/work'} LIVE_TS={tmp / 'ts2.txt'} {CORE / '.venv/bin/python'} "
            f"{CORE / 'scripts/live_panes_demo.py'}\n")
        resumed = ""
        for _ in range(100):
            st = [a.get("state") for a in d.agents("live")]
            if any(s in ("working", "done") for s in st):
                resumed = ",".join(map(str, st))
                break
            time.sleep(0.3)
        ok = bool(before) and before <= after and bool(resumed)
        emit(M, "After a tuios daemon restart the panes resume",
             how_live + "; kill-server, start daemon again, list-agents, re-run the agent in the restored pane",
             "PASS" if ok else "FAIL",
             f"windows before={sorted(i[:8] for i in before)} after restart={sorted(i[:8] for i in after)}\n"
             f"{tail(sess, 3)}\nrestored pane reported again: {resumed or 'NO'}")
    finally:
        d.stop()


def main() -> None:
    rc, out = go("-count=1 ./internal/k3keys/ ./internal/harness/")
    emit(M, "go test passes for k3keys and harness", "cd panes && go test -count=1 ./internal/k3keys/ ./internal/harness/",
         "PASS" if rc == 0 else "FAIL", out)
    rc, out = go("-count=1 -v -run 'TestHintsPerMode|TestModeLabel|TestAgentsModeKeys|TestEscFromEveryMode' ./internal/k3keys/")
    emit(M, "Hint bar is always correct", "k3keys tests: every mode has hints with key+label, essential esc in each "
         "non-typing mode, lock suffix, chooser lists all modes (TestHintsPerMode et al., -v)",
         "PASS" if rc == 0 and "--- PASS: TestHintsPerMode" in out else "FAIL",
         "\n".join(ln for ln in out.splitlines() if ln.startswith(("--- ", "ok", "FAIL"))))
    # keymap=tuios: the stock cmd/tuios binary never installs k3keys (only cmd/k3 does), so the whole upstream tree
    # runs with the tuios keymap. Everything except the k3-only package.
    pk = run("go list ./... | grep -v internal/k3keys", cwd=PANES)[1].split()
    rc, out = go("-count=1 " + " ".join(pk), timeout=3000)
    fails = [ln for ln in out.splitlines() if ln.startswith(("FAIL", "--- FAIL"))]
    npass = sum(1 for ln in out.splitlines() if ln.startswith("ok"))
    emit(M, "Upstream tuios tests pass with keymap=tuios",
         "cd panes && go test ./... (all packages but k3keys; cmd/tuios never installs k3keys so the tuios keymap is active)",
         "PASS" if rc == 0 else "FAIL", f"{npass} packages ok, {len(fails)} failing\n" + "\n".join(fails[:5]) + "\n" + tail(out, 3))
    tmp = Path(tempfile.mkdtemp(prefix="m3-exit-"))
    try:
        live_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    emit(M, "Hallway test: a first-time user drives panes from the hints alone", "needs a human", "PENDING",
         "not run", "Sit a first-time user in front of `k3` with no instructions, ask them to open 2 panes, "
         "switch tabs and open the Inbox using only the hint bar; record success/failure and stumbles in docs/reports")


if __name__ == "__main__":
    main()
