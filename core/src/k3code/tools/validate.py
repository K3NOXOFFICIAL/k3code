"""Tool-argument checks against a tool's JSON schema, before the handler runs.

A handler that indexed a missing argument used to surface as ``Tool execution failed: 'path'``, which told the model
nothing, so it retried blind. The subset checked here is what tool schemas in k3code use at the top level:
``required``, ``type`` (one or a list), ``enum`` and ``additionalProperties: false``. Anything else (nested schemas,
``anyOf``, ``$ref`` in MCP tools) is left to the tool.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
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


def check_arguments(schema: dict[str, Any] | None, args: Any) -> list[str]:
    """What is wrong with ``args`` for ``schema`` (empty = nothing found)."""
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        return []
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


#: Near-miss names a model sends for a built-in tool's argument: alias -> canonical. Only names whose meaning is
#: unambiguous for that tool; a call that sends the canonical name and an alias, or two aliases of one name, is an
#: error, never a guess. The permission engine reads ``path`` (or ``file``): resolving the aliases before it decides
#: means a protected path cannot be reached through a spelling it does not know.
_PATH_ALIASES = {"file_path": "path", "filepath": "path", "filename": "path", "file": "path"}
_DIR_ALIASES = {"dir": "path", "directory": "path"}
_JOB_ALIASES = {"id": "job_id", "shell_id": "job_id", "bash_id": "job_id"}
ALIASES: dict[str, dict[str, str]] = {
    "read": _PATH_ALIASES,
    "write": _PATH_ALIASES,
    "edit": {
        **_PATH_ALIASES,
        **dict.fromkeys(("old", "old_str", "old_text", "oldText", "search"), "old_string"),
        **dict.fromkeys(("new", "new_str", "new_text", "newText", "replace"), "new_string"),
    },
    "grep": {
        **dict.fromkeys(("regex", "query", "search"), "pattern"),
        **dict.fromkeys(("include_pattern", "glob"), "include"),
        **_DIR_ALIASES,
    },
    "glob": _DIR_ALIASES,
    "bash": {**dict.fromkeys(("cmd", "script", "shell"), "command"), "run_in_background": "background"},
    "bash_output": _JOB_ALIASES,
    "bash_kill": _JOB_ALIASES,
    "todo": {"items": "todos"},
    "task": {"task": "prompt", "subagent_type": "agent_type"},
    "web_search": {"q": "query"},
}
#: Built-in tools whose handler reads nothing but its schema's arguments (plus these older names): any other argument
#: is rejected, naming the accepted ones (``grep {dir: "src"}`` used to search "." and look like a success). MCP tools
#: are not listed: their server owns the schema.
STRICT: dict[str, frozenset[str]] = {
    name: frozenset(extra)
    for name, extra in {
        # ``encoding`` (read) and ``mode`` (write) are accepted and ignored: the handlers read neither (files are
        # read as UTF-8 with replacement and written as given), and models send them out of habit.
        "read": ("start", "end", "encoding"),
        "write": ("mode",),
        "edit": (),
        "grep": (),
        "glob": (),
        # ``description`` is a label Claude-Code-trained models send on nearly every call; the handler ignores it.
        "bash": ("description",),
        "bash_output": (),
        "bash_kill": (),
        "todo": (),
        "task": (),
        "task_result": (),
        "web_search": (),
        "web_fetch": (),
        "web_browse": (),
        "skill": (),
    }.items()
}
#: Path-like string arguments: surrounding whitespace and quotes, a ``file://`` scheme and a leading ``~`` are taken off
#: before anything (the permission engine included) sees them.
_PATHS: dict[str, tuple[str, ...]] = {
    "read": ("path",),
    "write": ("path",),
    "edit": ("path",),
    "grep": ("path",),
    "glob": ("path",),
    "bash": ("cwd",),
}
_URLS: dict[str, tuple[str, ...]] = {"web_fetch": ("url",), "web_browse": ("url",)}
#: Arguments that may not be empty: (name, blank counts as empty, what to send).
_NONEMPTY: dict[str, tuple[str, bool, str]] = {
    "bash": ("command", True, "the shell command to run"),
    "grep": ("pattern", False, "the text or regex to search for"),
    "web_search": ("query", True, "the words to search for"),
}
#: Keys the providers put in a call's arguments when what the model sent did not parse to a JSON object (a string that
#: is not JSON: ``_unparsed``; JSON that is not an object: ``_raw``). The model never sent these names.
_PROVIDER_MARKERS = ("_unparsed", "_raw")
_QUOTES = ('"', "'", "`")


def shown(value: Any, limit: int = 80) -> str:
    """``value`` for an error message: its repr, cut at ``limit`` chars."""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass(frozen=True)
class Normalized:
    """``normalize_arguments``' answer: the arguments to use (a new dict when anything changed, never the caller's
    dict mutated), what was changed, and the error to send instead of running the call."""

    args: Any
    changes: tuple[str, ...] = ()
    error: str | None = None


def _clean_path(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in _QUOTES:
        v = v[1:-1].strip()
    if v.startswith("file:///"):
        v = v[len("file://") :]
    if v == "~" or v.startswith("~/"):
        v = os.path.expanduser(v)
    return v


def _clean_url(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in _QUOTES:
        v = v[1:-1].strip()
    return v


def _rejects_null(prop: Any) -> bool:
    """True when ``prop`` declares a type and null is not among them."""
    if not isinstance(prop, dict):
        return False
    typ = prop.get("type")
    types = typ if isinstance(typ, list) else [typ] if isinstance(typ, str) else []
    return bool(types) and "null" not in types


def _coerce(value: Any, prop: Any) -> Any:
    """A string the schema wants as an integer, number or boolean, converted when it is exactly one; else unchanged."""
    if not isinstance(prop, dict) or not isinstance(value, str):
        return value
    typ = prop.get("type")
    types = typ if isinstance(typ, list) else [typ] if isinstance(typ, str) else []
    if not types or any(_is(value, t) for t in types):
        return value
    v = value.strip()
    if "boolean" in types and v.lower() in ("true", "false"):
        return v.lower() == "true"
    if ("integer" in types or "number" in types) and re.fullmatch(r"-?[0-9]+", v):
        return int(v)
    if "number" in types and re.fullmatch(r"-?([0-9]+\.[0-9]*|\.[0-9]+)", v):
        return float(v)
    return value


def normalize_arguments(tool: str, schema: dict[str, Any] | None, args: Any) -> Normalized:
    """``args`` with the safe repairs applied, or the error to send instead. Never raises, never mutates ``args``.

    For the built-in tools: an alias is renamed to its canonical name (only when that name is absent and exactly one
    alias is present), an unknown argument is rejected, path-like values lose surrounding whitespace/quotes, a
    ``file://`` scheme and a leading ``~``, a URL loses surrounding whitespace/quotes, and an empty command, pattern
    or query is rejected. For every tool: a string that is exactly a number or boolean the schema asks for is
    converted, and a null for an optional argument whose type does not allow null is dropped. Everything after this
    (schema check, permission engine, hooks, guards, handler) sees the result."""
    if not isinstance(args, dict) or not isinstance(schema, dict) or schema.get("type", "object") != "object":
        return Normalized(args)
    props = schema.get("properties") or {}
    for marker in _PROVIDER_MARKERS:
        if marker in args and marker not in props:
            return Normalized(
                args,
                error="your arguments were not a valid JSON object; send one JSON object with the fields: "
                + compact_schema(schema),
            )
    out = dict(args)
    changes: list[str] = []
    aliases = {a: c for a, c in ALIASES.get(tool, {}).items() if c in props}
    for canonical in dict.fromkeys(aliases.values()):
        sent = [a for a, c in aliases.items() if c == canonical and a in out]
        if not sent:
            continue
        if canonical in out:
            return Normalized(
                args,
                error=f"'{canonical}' and '{sent[0]}' both given for {tool} ('{sent[0]}' is another name for "
                f"'{canonical}'): send only '{canonical}'",
            )
        if len(sent) > 1:
            names = " and ".join(f"'{a}'" for a in sent)
            return Normalized(args, error=f"{names} both mean '{canonical}' for {tool}: send only '{canonical}'")
        out[canonical] = out.pop(sent[0])
        changes.append(f"read '{sent[0]}' as '{canonical}'")
    if tool in STRICT:
        accepted = [*props, *sorted(STRICT[tool])]
        unknown = [name for name in out if name not in accepted]
        if unknown:
            names = ", ".join(f"'{n}'" for n in unknown)
            near = [m for n in unknown for m in difflib.get_close_matches(n, accepted, n=1, cutoff=0.6)]
            guess = f" (did you mean {', '.join(repr(m) for m in near)}?)" if near else ""
            return Normalized(
                args,
                error=f"unknown argument {names} for {tool}{guess}; accepted: {', '.join(repr(a) for a in accepted)}",
            )
    for fields, clean in ((_PATHS.get(tool, ()), _clean_path), (_URLS.get(tool, ()), _clean_url)):
        for name in fields:
            value = out.get(name)
            if isinstance(value, str) and (new := clean(value)) != value:
                out[name] = new
                changes.append(f"'{name}' {shown(value)} as {shown(new)}")
    required = set(schema.get("required") or [])
    for name, value in list(out.items()):
        if value is None and name not in required and _rejects_null(props.get(name)):
            del out[name]  # a provider quirk: "offset": null for "not given"
            changes.append(f"dropped null '{name}'")
    for name, value in list(out.items()):
        if (new := _coerce(value, props.get(name))) is not value:
            out[name] = new
            changes.append(f"'{name}' {shown(value)} as {new!r}")
    if tool in _NONEMPTY:
        name, blank, what = _NONEMPTY[tool]
        value = out.get(name)
        if isinstance(value, str) and (value.strip() if blank else value) == "":
            return Normalized(args, error=f"'{name}' is empty for {tool}: send {what}")
    return Normalized(out if changes else args, tuple(changes))


def invalid_arguments(tool: str, schema: dict[str, Any] | None, args: Any) -> str | None:
    """The error the model gets for a call its tool's schema rejects, or None when the call passes."""
    problems = check_arguments(schema, args)
    if not problems:
        return None
    return f"invalid arguments for {tool}: {'; '.join(problems)} (expected: {compact_schema(schema or {})})"
