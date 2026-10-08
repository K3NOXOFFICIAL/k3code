"""Fake-provider demo: /ultracode on a temp git repo with 3 parallel subtasks. Prints an event summary + git graph.

Run: cd core && uv run python scripts/demo_ultracode.py
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))

from m1cmd_helpers import git_repo  # noqa: E402
from test_autonomy_gateway import call, make  # noqa: E402
from test_ultra import FIXER, JUDGE, LENS_C, LENS_S, PLANNERS, VOTE_C, VOTE_S, cfg, code_steps  # noqa: E402


class MP:  # minimal monkeypatch stand-in
    def setenv(self, k: str, v: str) -> None:
        os.environ[k] = v


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="k3demo-"))
    repo = git_repo(tmp / "repo")
    steps = code_steps(LENS_C, LENS_S, VOTE_C, VOTE_S, *FIXER)
    assert PLANNERS and JUDGE
    server = make(tmp, MP(), steps, mode="auto", autonomy=cfg())  # type: ignore[arg-type]
    await call(server, "session.create", {"cwd": str(repo)})
    out = await call(server, "slash.exec", {"command": "ultracode add three files"})
    print("command reply:", out["output"])
    await server.session.turn_task
    counts: collections.Counter[str] = collections.Counter()
    print("\n== event log (key events) ==")
    for line in server._frames:
        f = json.loads(line)
        if f.get("method") != "event":
            continue
        t, p = f["params"]["type"], f["params"]["payload"]
        counts[t] += 1
        if t == "ultra.progress":
            print(
                f"ultra.progress   {p['phase']:<26} {p.get('detail', '')}  [{p.get('agents')}/{p.get('max_agents')} agents]"
            )
        elif t == "fanout.plan":
            print(
                f"fanout.plan      {len(p['subtasks'])} subtasks, max_parallel={p['max_parallel']}, tests={p['test_command']}"
            )
        elif t == "fanout.progress" and p["state"] in ("merged", "retrying", "escalated"):
            print(f"fanout.progress  {p['subtask_id']} {p['state']:<9} ({p['done']}/{p['total']}) {p['title']}")
        elif t == "fanout.done":
            print(f"fanout.done      merged {p['merged']}/{p['total']}, tests {p['tests']}")
    print("\n== event counts ==")
    for k, v in sorted(counts.items()):
        if k.startswith(("subagent.", "fanout.", "ultra.", "plan.", "message.")):
            print(f"{k:<28}{v}")
    print("\n== final report ==")
    print(server.session.stored.messages[-1]["content"])
    print("\n== git log --graph --oneline ==")
    print(subprocess.run(["git", "log", "--graph", "--oneline"], cwd=repo, capture_output=True, text=True).stdout)
    print("repo:", repo)


asyncio.run(main())
