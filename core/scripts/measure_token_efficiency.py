"""Measure what a scripted tool loop sends to the provider: request bytes and prompt-cache breakpoints.

A fake provider plays a fixed 10-tool-call script (a 600-line file read twice, greps, a glob, small reads) and
serializes every request it is sent with the real Anthropic and OpenAI-compatible payload builders, without network.
It runs against whichever k3code is importable, so the same script measures two trees:

    cd core && uv run python scripts/measure_token_efficiency.py                    # this checkout
    PYTHONPATH=/path/to/other/core/src uv run python scripts/measure_token_efficiency.py   # another tree

Prints one JSON object with two scenarios:

- ``default``: the loop gets no context window, so nothing is elided; the second read of the unchanged file is the
  short "unchanged since" answer where the tree has it.
- ``tight_window``: the same 10 calls, but the big file is touched (new mtime, same text) before its second read, so
  that read returns the full file again, and the loop gets a context window of TIGHT_WINDOW tokens. Past half of it,
  results older than the last 6 calls are elided from the requests (``elided_results_per_request``). A tree whose
  AgentLoop takes no ``context_window`` runs the scenario without one (``context_window_used`` false).

``uncached_bytes`` approximates what is billed at the full input price: with cache breakpoints, each request pays only
for what it adds after the previous one (the rest is a cache read); without them, every request pays for all of it.
An elision rewrites an earlier part of the request, so the request after it pays for itself in full again.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="k3-measure-"))  # the project the loop works in
HOME = Path(tempfile.mkdtemp(prefix="k3-measure-home-"))  # outside WORK: greps must not find the transcript
os.environ["K3CODE_HOME"] = str(HOME)  # never the real ~/.k3code

from k3code.agent.loop import AgentLoop  # noqa: E402
from k3code.providers.anthropic import AnthropicProvider  # noqa: E402
from k3code.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from k3code.providers.types import Message, StreamEvent, ToolCall, Usage  # noqa: E402
from k3code.reliability import Reliability  # noqa: E402
from k3code.router import Router, build_chain  # noqa: E402

MODEL = "claude-measure"
#: Tokens of the tight_window scenario: the requests pass half of it once the big file has been read twice.
TIGHT_WINDOW = 16_000
SECOND_BIG_READ = 5  # the step of the second read of the big file
ELIDED = "result elided:"


def make_files() -> dict[str, str]:
    big = WORK / "big.py"
    big.write_text("".join(f"def handler_{i:03d}(request):  # row {i:03d}\n    return {i}\n\n" for i in range(200)))
    small = WORK / "notes.md"
    small.write_text("# notes\n\nhandler_150 is the slow one\n")
    assert len(big.read_text().splitlines()) == 600
    return {"big": str(big), "small": str(small)}


def script(files: dict[str, str]) -> list[ToolCall]:
    steps = [
        ("read", {"path": files["big"]}),
        ("grep", {"pattern": "handler_15", "path": str(WORK)}),
        ("read", {"path": files["small"]}),
        ("glob", {"pattern": "*.py", "path": str(WORK)}),
        ("grep", {"pattern": "return 1[0-9]$", "path": files["big"]}),
        ("read", {"path": files["big"]}),  # SECOND_BIG_READ: the same file again
        ("grep", {"pattern": "slow", "path": str(WORK)}),
        ("read", {"path": files["small"]}),
        ("glob", {"pattern": "*.md", "path": str(WORK)}),
        ("grep", {"pattern": "row 599", "path": files["big"]}),
    ]
    return [ToolCall(id=f"call_{i}", name=name, arguments=args) for i, (name, args) in enumerate(steps)]


class Measuring:
    """Answers with the script; serializes every request with the real payload builders."""

    name = "measure"
    base_url = "fake://measure"

    def __init__(self, calls: list[ToolCall], touch: str | None = None) -> None:
        self.calls = calls
        self.touch = touch  # a file to give a new mtime just before the second big read runs
        self.anthropic = AnthropicProvider(name="a", api_key="unused")
        self.openai = OpenAICompatProvider(name="o", base_url="https://relay.invalid/v1", api_key="unused")
        self.rows: list[dict[str, int | bool]] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        a = json.dumps(self.anthropic._payload(messages, tools, model, max_tokens=max_tokens, temperature=None))
        o = json.dumps(self.openai._payload(messages, tools, model, max_tokens=max_tokens, temperature=None))
        self.rows.append(
            {
                "anthropic_bytes": len(a),
                "openai_bytes": len(o),
                "cache_control": '"cache_control"' in a,
                "elided": a.count(ELIDED),
            }
        )
        step = len(self.rows) - 1
        if step == SECOND_BIG_READ and self.touch:
            st = os.stat(self.touch)
            os.utime(self.touch, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        if step < len(self.calls):
            msg = Message(role="assistant", content=None, tool_calls=[self.calls[step]])
        else:
            msg = Message(role="assistant", content="done", tool_calls=[])
        yield StreamEvent(type="done", message=msg, usage=Usage())

    async def aclose(self) -> None:
        await self.anthropic.aclose()
        await self.openai.aclose()


async def run(files: dict[str, str], *, window: int | None, touch: bool) -> dict:
    provider = Measuring(script(files), touch=files["big"] if touch else None)
    takes_window = "context_window" in inspect.signature(AgentLoop.__init__).parameters
    extra = {"context_window": window} if window and takes_window else {}
    session = f"measure-{window or 'default'}"  # one transcript per scenario: the second run must not resume the first
    loop = AgentLoop(
        Router(build_chain([provider], [[MODEL]]), max_retries=0),
        system_prompt="You are a coding agent. " * 200,  # ~5 kB, about what the composed prompt weighs
        max_turns=20,
        permission_mode="yolo",
        cwd=WORK,
        session=session,
        reliability=Reliability.from_settings(None, session=session, home=HOME),
        **extra,
    )
    async for _ in loop.run("Find the slow handler."):
        pass
    await provider.aclose()
    rows = provider.rows
    uncached, previous, previous_elided = 0, 0, 0
    for row in rows:
        size = int(row["anthropic_bytes"])
        reuses_prefix = row["cache_control"] and previous and int(row["elided"]) == previous_elided
        uncached += size - previous if reuses_prefix else size
        previous, previous_elided = size, int(row["elided"])
    out = {
        "requests": len(rows),
        "anthropic_bytes_total": sum(int(r["anthropic_bytes"]) for r in rows),
        "openai_bytes_total": sum(int(r["openai_bytes"]) for r in rows),
        "anthropic_bytes_per_request": [r["anthropic_bytes"] for r in rows],
        "cache_control_present": all(r["cache_control"] for r in rows),
        "anthropic_uncached_bytes_approx": uncached,
    }
    if window:
        out["context_window_used"] = bool(extra)
        out["elided_results_per_request"] = [r["elided"] for r in rows]
    return out


async def main() -> dict:
    files = make_files()
    default = await run(files, window=None, touch=False)
    files = make_files()  # fresh mtimes, so nothing carries over between the scenarios
    tight = await run(files, window=TIGHT_WINDOW, touch=True)
    return {"default": default, "tight_window": {"context_window": TIGHT_WINDOW, **tight}}


if __name__ == "__main__":
    import shutil

    try:
        print(json.dumps(asyncio.run(main()), indent=2))
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(HOME, ignore_errors=True)
