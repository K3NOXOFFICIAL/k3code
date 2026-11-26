"""Text the model reads names only tools it can call: a tool that does not exist (``write_file``) or is hidden in the
current mode (``exit_plan`` outside plan mode) sends it down a dead end."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

from k3code import ask_user, prompting
from k3code.agent.loop import AgentLoop
from k3code.automation.runner import TickContext
from k3code.automation.server_runner import _schedule_next_installer
from k3code.autonomy import advisor
from k3code.autonomy.plan_first import PLAN_ADDENDUM
from k3code.config import Settings
from k3code.extratools import register_mcp_tools, register_skill_tool
from k3code.permissions.engine import EXIT_PLAN_TOOL, PLAN_MSG
from k3code.research.tools import register_web_tools
from k3code.router import Router, build_chain
from k3code.subagents.tools import register_task_tools
from k3code.tools import build_registry, fuzzy_match

#: Words in the scanned text that look like tool names but are arguments, config keys or other non-tool identifiers.
#: Anything else must be a registered tool.
NOT_TOOLS = {
    # tool arguments named in prompts and messages
    "timeout",
    "background",
    "include",
    "query",
    "old_string",
    "new_string",
    "agent_type",
    "multiSelect",
    "isolation",
    "tier",
    "description",
    "prompt",
    "url",
    # shell programs and commands named in examples
    "pytest",
    "git",
    "commit",
    "rg",
    "ruff",
    "uv",
    "sandbox",
    # file or config names, not tools
    "sandbox.git_writable",
}

CODE_NAME = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
#: a bare snake_case word (with an underscore) followed by "tool", or one starting with a verb-like prefix:
#: how a message names a tool
BARE_TOOL = re.compile(
    r"\b([a-z]+(?:_[a-z]+)+)\s+tool\b"
    r"|\b((?:read|write|edit|delete|create)_[a-z]+)\b"
)
SRC = Path(prompting.__file__).parent


def _stub_mcp() -> SimpleNamespace:
    tool = SimpleNamespace(server="demo", name="lookup", qualified="mcp__demo__lookup", description="d", schema=None)
    return SimpleNamespace(tools=lambda: [tool], states=lambda: [], search=lambda q: [], call=None)


def all_specs() -> list:
    """Every ToolSpec a session can hold: the builtins plus what each module adds on top of them."""
    reg = build_registry()
    register_skill_tool(reg, Path("."), [])
    register_mcp_tools(reg, _stub_mcp())
    register_web_tools(reg, Settings())
    register_task_tools(reg, SimpleNamespace(subagents=None), None)
    _schedule_next_installer(TickContext())(reg)
    reg.register(advisor.TOOL_SPEC, None)
    reg.register(ask_user.TOOL_SPEC, None)
    return [spec for spec, _ in (reg.get(n) for n in reg.names())]


def _descriptions(node) -> list[str]:
    """Every ``description`` string in a JSON schema, nested ones included."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                found.append(value)
            else:
                found += _descriptions(value)
    elif isinstance(node, list):
        for item in node:
            found += _descriptions(item)
    return found


def _module_strings(path: Path) -> list[str]:
    """String constants of a module outside its docstrings: the text a function can hand to the model."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    ]


def model_facing_texts(specs: list | None = None) -> list[tuple[str, str]]:
    texts = []
    for spec in specs if specs is not None else all_specs():
        for desc in [spec.description, *_descriptions(spec.parameters)]:
            texts.append((f"tool {spec.name}", desc))
    for md in sorted((SRC / "prompts").glob("*.md")):
        texts.append((f"prompts/{md.name}", md.read_text(encoding="utf-8")))
    texts.append(("PLAN_ADDENDUM", PLAN_ADDENDUM))
    texts.append(("PLAN_MSG", PLAN_MSG))
    texts += [("fuzzy_match", s) for s in _module_strings(Path(fuzzy_match.__file__))]
    return texts


def unknown_names(texts: list[tuple[str, str]], registered: set[str]) -> set[tuple[str, str]]:
    bad = set()
    for where, text in texts:
        names = set(CODE_NAME.findall(text))
        names |= {a or b for a, b in BARE_TOOL.findall(text)}
        for name in names:
            if name and name not in registered and name not in NOT_TOOLS and not name.startswith("mcp__"):
                bad.add((where, name))
    return bad


def test_tool_descriptions_and_messages_name_only_registered_tools():
    specs = all_specs()
    registered = {s.name for s in specs}
    assert {"read", "write", "exit_plan", "skill", "web_fetch", "task_result", "advisor", "schedule_next"} <= registered
    assert unknown_names(model_facing_texts(specs), registered) == set()


def _loop(tmp_path, mode: str) -> AgentLoop:
    router = Router(build_chain([SimpleNamespace(name="p")], [["m"]]), max_retries=0)
    return AgentLoop(router, system_prompt="s", permission_mode=mode, cwd=tmp_path)


def test_plan_mode_only_text_names_exit_plan_conditionally(tmp_path):
    # exit_plan is registered, but only offered in plan mode
    assert EXIT_PLAN_TOOL in _loop(tmp_path, "plan").tools.names()
    assert EXIT_PLAN_TOOL in {s.name for s in _loop(tmp_path, "plan").tool_specs()}
    assert EXIT_PLAN_TOOL not in {s.name for s in _loop(tmp_path, "default").tool_specs()}
    # the text that is always sent may name it only as available "if plan mode is active", never as a plain order
    plan_only = {EXIT_PLAN_TOOL, "PLAN_ADDENDUM", "PLAN_MSG"}
    for where, text in model_facing_texts():
        if EXIT_PLAN_TOOL in text and where.removeprefix("tool ") not in plan_only:
            assert re.search(r"[Ii]f plan mode is active[^.]*available[^.]*exit_plan", text), where
