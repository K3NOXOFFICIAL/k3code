"""Argument names and string-typed numbers or arrays that models send (Claude Code's spellings, stringified JSON):
renamed or read as the value they spell when that is unambiguous, refused as before when it is not."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from k3code.agent.loop import AgentLoop
from k3code.providers.types import ToolCall
from k3code.router import Router, build_chain
from k3code.tools.validate import check_arguments, invalid_arguments, normalize_arguments


class _NoProvider:
    name = "fake"
    base_url = "https://fake.test"

    async def stream(self, *a, **kw):  # pragma: no cover - these tests never call the model
        if False:
            yield None

    async def aclose(self) -> None:
        pass


def _loop(cwd: Path) -> AgentLoop:
    return AgentLoop(
        Router(build_chain([_NoProvider()], [["m"]]), max_retries=0), system_prompt="t", permission_mode="yolo", cwd=cwd
    )


def _schema(**props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props}


# ── Claude Code's names for a background job ──


@pytest.mark.parametrize("tool", ["bash_output", "bash_kill"])
@pytest.mark.parametrize("name", ["bash_id", "shell_id"])
def test_a_background_job_id_sent_under_claude_codes_name_is_the_job_id(tmp_path, tool, name):
    spec = _loop(tmp_path).tools.get(tool)[0]
    args = normalize_arguments(tool, {name: "job1"}, spec.parameters)
    assert args == {"job_id": "job1"}
    assert invalid_arguments(tool, spec.parameters, args) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "name"), [("bash_output", "bash_id"), ("bash_kill", "shell_id")])
async def test_the_call_reaches_the_job_registry_instead_of_missing_job_id(tmp_path, tool, name):
    result = await _loop(tmp_path)._execute_tool(ToolCall(id="c", name=tool, arguments={name: "job99"}))
    assert "missing required" not in result["error"]
    assert result["error"].startswith("no job 'job99' in this session")


def test_the_real_name_wins_over_an_alias_sent_with_it(tmp_path):
    spec = _loop(tmp_path).tools.get("bash_output")[0]
    args = normalize_arguments("bash_output", {"bash_id": "job1", "job_id": "job2"}, spec.parameters)
    assert args["job_id"] == "job2" and args["bash_id"] == "job1"


# ── numbers and arrays sent as text ──

INT = _schema(offset={"type": "integer"})
NUMBER = _schema(ratio={"type": "number"})
ARRAY = _schema(todos={"type": "array", "items": {"type": "object"}})


@pytest.mark.parametrize(
    ("schema", "sent", "expected"),
    [
        (INT, {"offset": "3"}, {"offset": 3}),
        (INT, {"offset": " 42 "}, {"offset": 42}),
        (INT, {"offset": "-7"}, {"offset": -7}),
        (INT, {"offset": "+5"}, {"offset": 5}),
        (NUMBER, {"ratio": "2"}, {"ratio": 2}),
        (NUMBER, {"ratio": "2.5"}, {"ratio": 2.5}),
        (NUMBER, {"ratio": "-.5"}, {"ratio": -0.5}),
        (NUMBER, {"ratio": "1e3"}, {"ratio": 1000.0}),
        (
            ARRAY,
            {"todos": '[{"content": "a", "status": "pending"}]'},
            {"todos": [{"content": "a", "status": "pending"}]},
        ),
        (ARRAY, {"todos": " [] "}, {"todos": []}),
        (_schema(offset={"type": ["integer", "null"]}), {"offset": "3"}, {"offset": 3}),
    ],
)
def test_a_number_or_array_sent_as_text_is_read_as_the_value_it_spells(schema, sent, expected):
    args = normalize_arguments("t", sent, schema)
    assert args == expected and type(args[next(iter(args))]) is type(expected[next(iter(expected))])
    assert check_arguments(schema, args) == []


@pytest.mark.parametrize(
    ("schema", "sent"),
    [
        (INT, {"offset": "3.5"}),
        (INT, {"offset": "1e3"}),
        (INT, {"offset": "abc"}),
        (INT, {"offset": ""}),
        (INT, {"offset": "0x10"}),
        (INT, {"offset": "1_000"}),
        (INT, {"offset": "٣"}),  # a non-ASCII digit
        (INT, {"offset": "3 4"}),
        (NUMBER, {"ratio": "NaN"}),
        (NUMBER, {"ratio": "inf"}),
        (NUMBER, {"ratio": "-Infinity"}),
        (NUMBER, {"ratio": "1e999"}),
        (NUMBER, {"ratio": "2.5.1"}),
        (ARRAY, {"todos": '[{"content": "a"'}),  # does not parse
        (ARRAY, {"todos": '{"content": "a"}'}),  # an object, not an array
        (ARRAY, {"todos": "a, b"}),
        (ARRAY, {"todos": "[1, 2] and more"}),
        (ARRAY, {"todos": ""}),
    ],
)
def test_text_that_does_not_read_cleanly_as_the_value_is_refused_as_before(schema, sent):
    args = normalize_arguments("t", sent, schema)
    assert args == sent
    (name,) = sent
    assert any(f"'{name}' must be" in p and "got str" in p for p in check_arguments(schema, args))


def test_a_parameter_that_takes_a_string_is_never_coerced():
    schema = _schema(
        content={"type": "string"},
        either={"type": ["string", "integer"]},
        listing={"type": ["string", "array"]},
        free={},
        count={"type": "integer"},
    )
    sent = {"content": "5", "either": "7", "listing": "[1, 2]", "free": "9", "count": "5"}
    assert normalize_arguments("t", sent, schema) == {**sent, "count": 5}
    assert normalize_arguments("t", {"unknown": "5"}, schema) == {"unknown": "5"}


def test_the_item_types_of_a_coerced_array_are_still_checked():
    schema = _schema(paths={"type": "array", "items": {"type": "string"}})
    args = normalize_arguments("t", {"paths": '["a", 1]'}, schema)
    assert args == {"paths": ["a", 1]}
    assert check_arguments(schema, args) == ["'paths[1]' must be string, got int"]
    assert check_arguments(schema, normalize_arguments("t", {"paths": '["a", "b"]'}, schema)) == []


def test_without_a_schema_nothing_is_coerced():
    assert normalize_arguments("t", {"offset": "3"}, None) == {"offset": "3"}
    assert normalize_arguments("t", {"offset": "3"}, {"type": "object"}) == {"offset": "3"}
    assert normalize_arguments("t", {"_unparsed": "[1]"}, INT) == {"_unparsed": "[1]"}


def test_the_arguments_dict_the_model_sent_is_not_changed():
    sent = {"offset": "3"}
    assert normalize_arguments("t", sent, INT) == {"offset": 3} and sent == {"offset": "3"}


@pytest.mark.asyncio
async def test_read_with_an_offset_and_limit_sent_as_text_reads_those_lines(tmp_path):
    (tmp_path / "a.txt").write_text("".join(f"line{i}\n" for i in range(1, 8)))
    call = ToolCall(id="c", name="read", arguments={"path": "a.txt", "offset": "3", "limit": "2"})
    result = await _loop(tmp_path)._execute_tool(call)
    assert "error" not in result, result
    text = str(result["content"])
    assert "line3" in text and "line4" in text and "line2" not in text and "line5" not in text


@pytest.mark.asyncio
async def test_todo_with_a_stringified_list_is_taken(tmp_path):
    args = {"todos": '[{"content": "write tests", "status": "in_progress", "activeForm": "writing tests"}]'}
    result = await _loop(tmp_path)._execute_tool(ToolCall(id="c", name="todo", arguments=args))
    assert "error" not in result and "Todos updated" in str(result["content"]), result
