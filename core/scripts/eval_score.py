"""Score the harness on the offline eval suite: pass rate and model calls per scripted task.

Runs ``tests/test_agent_evals.py`` (no model, no network, a few seconds) and prints one row per task and the totals.
Run it before and after a prompt or tool change and compare the two scoreboards.

    cd core && uv run python scripts/eval_score.py                  # the table
    cd core && uv run python scripts/eval_score.py --json out.json  # also keep the raw scoreboard

Exits 1 when a task fails.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", type=Path, help="write the scoreboard JSON here too")
    opts = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "scoreboard.json"
        env = {**os.environ, "K3CODE_EVAL_OUT": str(out)}
        cmd = [sys.executable, "-m", "pytest", "tests/test_agent_evals.py", "-q", "-p", "no:cacheprovider"]
        subprocess.run(cmd, cwd=CORE, env=env, capture_output=True, text=True)
        if not out.exists():
            print("the eval suite did not write a scoreboard: run it with pytest to see why", file=sys.stderr)
            return 2
        board = json.loads(out.read_text())
    if opts.json:
        opts.json.write_text(json.dumps(board, indent=2))
    for row in board["tasks"]:
        mark = "pass" if row["passed"] else "FAIL"
        print(
            f"{row['id']:<28} {mark}  {row['steps']}/{row['max_steps']} steps  {'' if row['passed'] else row['detail']}"
        )
    print(f"eval pass rate: {board['passed']}/{board['total']}, mean steps: {board['mean_steps']:.2f}")
    return 0 if board["passed"] == board["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
