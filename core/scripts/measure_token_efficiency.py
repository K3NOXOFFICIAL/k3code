"""Measure what a scripted tool loop sends to the provider: request bytes and prompt-cache breakpoints.

A fake provider plays a fixed 10-tool-call script (a 600-line file read twice, greps, a glob, small reads) and
serializes every request it is sent with the real Anthropic and OpenAI-compatible payload builders, without network.
It runs against whichever k3code is importable, so the same script measures two trees:

    cd core && uv run python scripts/measure_token_efficiency.py                    # this checkout
    PYTHONPATH=/path/to/other/core/src uv run python scripts/measure_token_efficiency.py   # another tree

Prints one JSON object. ``uncached_bytes`` approximates what is billed at the full input price: with cache
breakpoints, each request pays only for what it adds after the previous one (the rest is a cache read); without
them, every request pays for all of it.
"""

from __future__ import annotations

import asyncio
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
        ("read", {"path": files["big"]}),  # the same file again, unchanged
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

    def __init__(self, calls: list[ToolCall]) -> None:
        self.calls = calls
        self.anthropic = AnthropicProvider(name="a", api_key="unused")
        self.openai = OpenAICompatProvider(name="o", base_url="https://relay.invalid/v1", api_key="unused")
        self.rows: list[dict[str, int | bool]] = []

    async def stream(self, messages, tools, model, *, max_tokens=8192, temperature=None):
        a = json.dumps(self.anthropic._payload(messages, tools, model, max_tokens=max_tokens, temperature=None))
        o = json.dumps(self.openai._payload(messages, tools, model, max_tokens=max_tokens, temperature=None))
        self.rows.append({"anthropic_bytes": len(a), "openai_bytes": len(o), "cache_control": '"cache_control"' in a})
        step = len(self.rows) - 1
        if step < len(self.calls):
            msg = Message(role="assistant", content=None, tool_calls=[self.calls[step]])
        else:
            msg = Message(role="assistant", content="done", tool_calls=[])
        yield StreamEvent(type="done", message=msg, usage=Usage())

    async def aclose(self) -> None:
        await self.anthropic.aclose()
        await self.openai.aclose()


async def main() -> dict:
    provider = Measuring(script(make_files()))
    loop = AgentLoop(
        Router(build_chain([provider], [[MODEL]]), max_retries=0),
        system_prompt="You are a coding agent. " * 200,  # ~5 kB, about what the composed prompt weighs
        max_turns=20,
        permission_mode="yolo",
        cwd=WORK,
        session="measure",
        reliability=Reliability.from_settings(None, session="measure", home=HOME),
    )
    async for _ in loop.run("Find the slow handler."):
        pass
    await provider.aclose()
    rows = provider.rows
    uncached, previous = 0, 0
    for row in rows:
        size = int(row["anthropic_bytes"])
        uncached += size - previous if row["cache_control"] and previous else size
        previous = size
    return {
        "requests": len(rows),
        "anthropic_bytes_total": sum(int(r["anthropic_bytes"]) for r in rows),
        "openai_bytes_total": sum(int(r["openai_bytes"]) for r in rows),
        "anthropic_bytes_per_request": [r["anthropic_bytes"] for r in rows],
        "cache_control_present": all(r["cache_control"] for r in rows),
        "anthropic_uncached_bytes_approx": uncached,
    }


if __name__ == "__main__":
    import shutil

    try:
        print(json.dumps(asyncio.run(main()), indent=2))
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(HOME, ignore_errors=True)
