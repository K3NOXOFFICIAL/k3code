"""Tool-argument checks against a tool's JSON schema, before the handler runs.

A handler that indexed a missing argument used to surface as ``Tool execution failed: 'path'``, which told the model
nothing, so it retried blind. The subset checked here is what tool schemas in k3code use at the top level:
``required``, ``type`` (one or a list), ``enum`` and ``additionalProperties: false``. Anything else (nested schemas,
``anyOf``, ``$ref`` in MCP tools) is left to the tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _is(value: Any, typ: str) -> bool:
    if typ == "null":
        return value is None
    if typ in ("integer", "number") and isinstance(value, bool):
        return False  # True is an int in Python, never in JSON
    if typ == "integer" and isinstance(value, float):
        return value.is_integer()
    py = _TYPES.get(typ)
    return True if py is None else isinstance(value, py)


def _type_name(prop: Any) -> str:
    if not isinstance(prop, dict):
        return "any"
    if "enum" in prop and isinstance(prop["enum"], list):
        return "|".join(repr(v) if not isinstance(v, str) else v for v in prop["enum"][:6])
    typ = prop.get("type")
    if isinstance(typ, list):
        return "|".join(str(t) for t in typ)
    return str(typ or "any")


def compact_schema(schema: dict[str, Any]) -> str:
    """``{path: string, offset?: integer}``: the arguments a tool takes, optional ones marked with ``?``."""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts = [f"{name}{'' if name in required else '?'}: {_type_name(p)}" for name, p in props.items()]
    return "{" + ", ".join(parts) + "}"


def unparsed_marker(args: Any) -> str | None:
    """The raw text of arguments a provider could not parse as a JSON object (it sends ``{"_unparsed": raw}`` for
    broken JSON and ``{"_raw": value}`` for JSON that is no object), else None."""
    if isinstance(args, dict) and len(args) == 1:
        for key in ("_unparsed", "_raw"):
            if key in args:
                return str(args[key])
    return None


def drop_null_options(args: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """``args`` without the optional arguments the model sent as ``null``: models fill unused options with null, which
    the schema (``type: integer``) rejects and the handlers do not expect. A required argument, and a property whose
    schema allows null, keep theirs."""
    if not isinstance(schema, dict) or not isinstance(args, dict) or None not in args.values():
        return args
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    def keeps_null(name: str) -> bool:
        prop = props.get(name)
        typ = prop.get("type") if isinstance(prop, dict) else None
        return name in required or not isinstance(prop, dict) or typ is None or typ == "null" or "null" in (typ or [])

    return {k: v for k, v in args.items() if v is not None or keeps_null(k)}


#: Names models use for a parameter these tools call something else (Claude Code's spellings): renamed instead of
#: dropped, so the call runs with the meaning the model gave it. A dropped ``run_in_background`` held a dev server
#: call for its whole 120 s timeout, a dropped ``subagent_type`` ran the sub-agent as the default worker.
ARGUMENT_ALIASES: dict[str, dict[str, str]] = {
    "bash": {"run_in_background": "background"},
    "task": {"subagent_type": "agent_type"},
    "todo": {"items": "todos"},
    "read": {"file_path": "path"},
    "write": {"file_path": "path"},
    "edit": {"file_path": "path", "old_str": "old_string", "new_str": "new_string"},
    "skill": {"skill": "name"},
    "grep": {"glob": "include"},
}
#: Arguments a handler still takes that its schema no longer lists (so they are not "ignored").
LEGACY_ARGUMENTS: dict[str, frozenset[str]] = {"read": frozenset({"start", "end"})}


def normalize_arguments(tool: str, args: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """``args`` as the handler should get them: null options dropped, aliased names renamed (unless the model sent the
    real name too, then the alias stays and is reported as ignored), grep's ``-i`` as ``(?i)``. An aliased name sent
    as null is an unused option like any other: it is dropped here, not renamed into a null that the real name's type
    rejects."""
    args = drop_null_options(args, schema)
    if tool == "grep" and isinstance(args, dict) and isinstance(args.get("-i"), bool):
        # Claude Code's case flag has no argument here: the regex takes it inline. Left as an ignored argument, the
        # search ran case-sensitively and answered "no matches" for a word that is there
        ignore_case = args["-i"]
        args = {k: v for k, v in args.items() if k != "-i"}
        if ignore_case and isinstance(args.get("pattern"), str) and not args["pattern"].startswith("(?i)"):
            args["pattern"] = "(?i)" + args["pattern"]
    aliases = ARGUMENT_ALIASES.get(tool)
    if not aliases or not isinstance(args, dict) or not any(k in aliases for k in args):
        return args
    return {
        (aliases[k] if k in aliases and aliases[k] not in args else k): v
        for k, v in args.items()
        if v is not None or k not in aliases
    }


def check_arguments(schema: dict[str, Any] | None, args: Any, *, local_paths: bool = True) -> list[str]:
    """What is wrong with ``args`` for ``schema`` (empty = nothing found). ``local_paths``: a path argument names a file
    of this machine; an MCP tool's may name one on the server (``~deploy/app.log``), so only what no path can hold is
    refused for it."""
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        return []
    if (raw := unparsed_marker(args)) is not None:
        shown = raw[:150] + ("…" if len(raw) > 150 else "")
        why = ""
        if "_unparsed" in args:  # broken JSON: say where, so the model repairs it instead of resending it
            try:
                json.loads(raw, strict=False)  # the providers' parsers are lenient about raw control characters too
            except json.JSONDecodeError as exc:
                why = f": {exc.msg} (char {exc.pos})"
        return [f"the arguments were not a valid JSON object{why} (received: {shown})"]
    if not isinstance(args, dict):
        return [f"arguments must be an object, got {type(args).__name__}"]
    props = schema.get("properties") or {}
    problems = [f"missing required '{name}'" for name in schema.get("required") or [] if name not in args]
    for name, value in args.items():
        prop = props.get(name)
        if not isinstance(prop, dict):
            if prop is None and schema.get("additionalProperties") is False:
                problems.append(f"unexpected argument '{name}'")
            continue
        typ = prop.get("type")
        types = typ if isinstance(typ, list) else [typ] if isinstance(typ, str) else []
        if types and not any(_is(value, t) for t in types):
            problems.append(f"'{name}' must be {'|'.join(types)}, got {type(value).__name__}")
            continue
        enum = prop.get("enum")
        if isinstance(enum, list) and value not in enum:
            problems.append(f"'{name}' must be one of {enum!r}, got {value!r}")
        item_type = (prop.get("items") or {}).get("type") if isinstance(prop.get("items"), dict) else None
        if isinstance(value, list | tuple) and isinstance(item_type, str):
            problems += [
                f"'{name}[{i}]' must be {item_type}, got {type(item).__name__}"
                for i, item in enumerate(value)
                if not _is(item, item_type)
            ]
        if name in PATH_ARGUMENTS and isinstance(value, str) and (bad := _unusable_path(value, local_paths)):
            problems.append(f"'{name}' {bad}")
    return problems


#: Arguments that name a file or directory: a value the operating system cannot take as one is refused up front (it
#: raised out of the permission check and ended the whole turn in an error, with the call marked as possibly run).
PATH_ARGUMENTS = frozenset({"path", "cwd", "file_path", "notebook_path"})


def _unusable_path(value: str, local: bool = True) -> str:
    """Why ``value`` can never be a path ("" when it can); ``local``: a path of this machine, where ``~name`` must be a
    user (Path.expanduser raises for one that is not)."""
    if "\x00" in value:
        return "contains a NUL byte"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "contains an unpaired surrogate character"
    if local and value.startswith("~") and not value.startswith(("~/", "~\\")) and value != "~":
        try:
            Path(value).expanduser()
        except RuntimeError:
            return f"starts with {value.split('/', 1)[0]!r}, which is no user's home directory"
    return ""


def invalid_arguments(tool: str, schema: dict[str, Any] | None, args: Any) -> str | None:
    """The error the model gets for a call its tool's schema rejects, or None when the call passes."""
    problems = check_arguments(schema, args, local_paths=not tool.startswith("mcp__"))
    if not problems:
        return None
    return f"invalid arguments for {tool}: {'; '.join(problems)} (expected: {compact_schema(schema or {})})"
