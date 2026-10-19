"""Names Claude-Code-trained models send, JSON nulls, provider markers and digits, through ``normalize_arguments``."""

from __future__ import annotations

from typing import Any

import pytest

from k3code.tools.validate import normalize_arguments

_SCHEMAS: dict[str, dict[str, Any]] = {
    "read": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
        "required": ["path"],
    },
    "write": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    "bash": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "integer"},
            "background": {"type": "boolean"},
        },
        "required": ["command"],
    },
    "bash_output": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    "bash_kill": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    "task": {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "prompt": {"type": "string"},
            "agent_type": {"type": "string"},
        },
        "required": ["description", "prompt"],
    },
}


def _n(tool: str, args: dict[str, Any]):
    return normalize_arguments(tool, _SCHEMAS[tool], args)


@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("bash", {"command": "ls", "description": "list"}, {"command": "ls", "description": "list"}),
        ("bash", {"command": "ls", "run_in_background": True}, {"command": "ls", "background": True}),
        ("bash_output", {"bash_id": "j1"}, {"job_id": "j1"}),
        ("bash_kill", {"bash_id": "j1"}, {"job_id": "j1"}),
        (
            "task",
            {"description": "d", "prompt": "p", "subagent_type": "explorer"},
            {"description": "d", "prompt": "p", "agent_type": "explorer"},
        ),
        ("read", {"path": "a.py", "encoding": "utf-8"}, {"path": "a.py", "encoding": "utf-8"}),
        ("write", {"path": "a", "content": "x", "mode": "w"}, {"path": "a", "content": "x", "mode": "w"}),
    ],
)
def test_claude_code_names_are_accepted_or_renamed(tool: str, args: dict[str, Any], expected: dict[str, Any]) -> None:
    norm = _n(tool, args)
    assert norm.error is None
    assert norm.args == expected


def test_both_spellings_of_background_are_an_error() -> None:
    assert _n("bash", {"command": "ls", "background": False, "run_in_background": True}).error


def test_other_claude_code_only_options_stay_rejected() -> None:
    assert "unknown argument 'description'" in (_n("read", {"path": "a", "description": "x"}).error or "")
    assert "unknown argument 'mode'" in (_n("read", {"path": "a", "mode": "r"}).error or "")


@pytest.mark.parametrize("marker,value", [("_unparsed", '{"path": "a.py'), ("_raw", ["a.py"])])
@pytest.mark.parametrize("tool", ["read", "bash"])
def test_a_provider_marker_is_answered_as_invalid_json_not_an_unknown_argument(
    tool: str, marker: str, value: Any
) -> None:
    norm = _n(tool, {marker: value})
    assert norm.error is not None
    assert "not a valid JSON object" in norm.error
    assert "unknown argument" not in norm.error and marker not in norm.error
    assert "path" in norm.error or "command" in norm.error  # the fields to send


def test_null_for_an_optional_property_is_dropped_and_recorded() -> None:
    norm = _n("read", {"path": "a.py", "offset": None, "limit": None})
    assert norm.error is None
    assert norm.args == {"path": "a.py"}
    assert "dropped null 'offset'" in norm.changes and "dropped null 'limit'" in norm.changes


def test_null_for_a_required_property_is_kept() -> None:
    assert _n("read", {"path": None}).args == {"path": None}


def test_null_stays_when_the_schema_allows_it() -> None:
    schema = {"type": "object", "properties": {"x": {"type": ["integer", "null"]}}}
    assert normalize_arguments("mcp__s__t", schema, {"x": None}).args == {"x": None}


def test_unicode_digits_are_not_numbers() -> None:
    assert _n("read", {"path": "a", "offset": "١٠"}).args["offset"] == "١٠"
    assert _n("read", {"path": "a", "offset": "10"}).args["offset"] == 10
