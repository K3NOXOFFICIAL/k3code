"""E5: bash documents its default and maximum timeout and says how to go on after one; T6: every tool states limits.

Kept apart from test_bash_jobs.py on purpose: these import only names that existed before background jobs, so
without the change they fail on what they assert, not at import.
"""

from __future__ import annotations

from k3code.config import Settings
from k3code.research.tools import MAX_FETCH_CHARS, register_web_tools
from k3code.tools import build_registry, tool_bash


async def test_timeout_says_how_long_and_how_to_go_on(tmp_path):
    res = await tool_bash({"command": "sleep 5", "timeout": 0.3}, cwd=tmp_path)
    assert res["error"] == "Command timed out after 0.3s; retry with timeout up to 600 or background: true"


def test_bash_schema_documents_default_and_max_timeout():
    spec, _ = build_registry().get("bash")
    timeout = spec.parameters["properties"]["timeout"]
    assert timeout["default"] == 30 and "default 30, max 600" in timeout["description"]
    assert "Seconds" in timeout["description"] and spec.parameters["properties"]["background"]["type"] == "boolean"


def test_every_builtin_tool_states_its_limits():
    reg = build_registry()
    register_web_tools(reg, Settings())
    for name in ("read", "write", "edit", "bash", "bash_output", "bash_kill", "grep", "glob", "todo", "web_fetch"):
        spec, _ = reg.get(name)
        limits = [ln for ln in spec.description.splitlines() if ln.startswith("Limits: ")]
        assert len(limits) == 1, name
    assert "30 s by default" in reg.get("bash")[0].description
    assert f"{MAX_FETCH_CHARS} chars" in reg.get("web_fetch")[0].description
