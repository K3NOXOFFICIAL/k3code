"""Tool-argument checks against a tool's JSON schema, before the handler runs.

A handler that indexed a missing argument used to surface as ``Tool execution failed: 'path'``, which told the model
nothing, so it retried blind. The subset checked here is what tool schemas in k3code use at the top level:
``required``, ``type`` (one or a list), ``enum`` and ``additionalProperties: false``. Anything else (nested schemas,
``anyOf``, ``$ref`` in MCP tools) is left to the tool.
"""

from __future__ import annotations

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


def check_arguments(schema: dict[str, Any] | None, args: Any) -> list[str]:
    """What is wrong with ``args`` for ``schema`` (empty = nothing found)."""
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        return []
    if (raw := unparsed_marker(args)) is not None:
        shown = raw[:150] + ("…" if len(raw) > 150 else "")
        return [f"the arguments were not a valid JSON object (received: {shown})"]
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
    return problems


def invalid_arguments(tool: str, schema: dict[str, Any] | None, args: Any) -> str | None:
    """The error the model gets for a call its tool's schema rejects, or None when the call passes."""
    problems = check_arguments(schema, args)
    if not problems:
        return None
    return f"invalid arguments for {tool}: {'; '.join(problems)} (expected: {compact_schema(schema or {})})"
