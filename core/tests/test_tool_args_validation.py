"""E2: tool arguments are checked against the tool's schema before the handler (and the approval prompt) run."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code.agent.loop import AgentLoop, ApprovalResult
from k3code.providers.types import ToolCall, ToolSpec
from k3code.router import Router, build_chain
from k3code.tools.validate import check_arguments, compact_schema


class _NoProvider:
    name = "fake"
    base_url = "https://fake.test"

    async def stream(self, *a, **kw):  # pragma: no cover - these tests never call the model
        if False:
            yield None

    async def aclose(self) -> None:
        pass


def _loop(cwd: Path, mode: str = "yolo", **kw) -> AgentLoop:
    return AgentLoop(
        Router(build_chain([_NoProvider()], [["m"]]), max_retries=0),
        system_prompt="t",
        permission_mode=mode,
        cwd=cwd,
        **kw,
    )


@pytest.mark.asyncio
async def test_missing_required_argument_names_it_and_the_schema(tmp_path):
    result = await _loop(tmp_path)._execute_tool(ToolCall(id="c", name="read", arguments={"offset": 3}))
    assert result["error"].startswith("invalid arguments for read: missing required 'path' (expected: {path: string")
    assert "offset?: integer" in result["error"]
    assert "Tool execution failed" not in result["error"]


@pytest.mark.asyncio
async def test_wrong_type_is_reported_before_the_handler_runs(tmp_path):
    result = await _loop(tmp_path)._execute_tool(
        ToolCall(id="c", name="write", arguments={"path": "a.txt", "content": 5})
    )
    assert "'content' must be string, got int" in result["error"]
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_invalid_call_never_asks_for_approval(tmp_path):
    asked: list[str] = []

    async def approve(tool, args, decision):
        asked.append(tool)
        return ApprovalResult("once")

    loop = _loop(tmp_path, mode="ask", headless=False, approval_callback=approve)
    result = await loop._execute_tool(ToolCall(id="c", name="bash", arguments={"cmd": "ls"}))
    assert "missing required 'command'" in result["error"]
    assert asked == []


@pytest.mark.asyncio
async def test_handler_exception_reports_type_and_message(tmp_path):
    loop = _loop(tmp_path)

    async def boom(arguments, *, cwd=None):
        raise ValueError("bad thing")

    loop.tools.register(ToolSpec(name="boom", description="", parameters={"type": "object"}), boom)
    result = await loop._execute_tool(ToolCall(id="c", name="boom", arguments={}))
    assert result == {"error": "Tool execution failed: ValueError: bad thing"}


def test_validator_subset():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"]},
            "n": {"type": "integer"},
            "x": {"type": ["string", "array"]},
            "flag": {"type": "boolean"},
        },
        "required": ["mode"],
        "additionalProperties": False,
    }
    assert check_arguments(schema, {"mode": "a", "n": 2.0, "x": ["q"], "flag": False}) == []
    assert check_arguments(schema, {"mode": "c"}) == ["'mode' must be one of ['a', 'b'], got 'c'"]
    assert check_arguments(schema, {"mode": "a", "n": True}) == ["'n' must be integer, got bool"]
    assert check_arguments(schema, {"mode": "a", "zzz": 1}) == ["unexpected argument 'zzz'"]
    assert check_arguments({"type": "object", "properties": {}}, {"zzz": 1}) == []  # extras allowed by default
    assert compact_schema(schema) == "{mode: a|b, n?: integer, x?: string|array, flag?: boolean}"
