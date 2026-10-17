"""Prompt back-ends: interactive (prompt_toolkit arrow keys) and answers-file (non-interactive)."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

import yaml


class AnswersError(ValueError):
    """An answers file that cannot be trusted: shown as a usage error, never as a traceback or a silent default."""


#: What a field of an answers file holds. ``ANY`` is not checked here (the step that reads it decides).
VALUE, LIST, MAP, ANY = "a single value", "a list", "a mapping", "anything"
_TYPES: dict[str, tuple[type, ...]] = {
    VALUE: (str, int, float, bool, type(None)),
    LIST: (list,),
    MAP: (dict,),
}
#: Every key an answers file may set, ``step: {field: kind}``; a step whose whole value is ``ANY`` is not looked into.
#: A field ``*`` takes any name (a provider or an environment variable). The keys come from the setup steps and
#: ``k3code onboard``; test_answers_validation.py fails when a step reads a key that is not listed here.
ANSWER_KEYS: dict[str, dict[str, str] | str] = {
    "onboard": dict.fromkeys(("mode", "provider", "endpoint", "key", "keep_key", "save_env_key", "model"), VALUE),
    "welcome": {"mode": VALUE, "bundle": VALUE, "accept": MAP},
    "about": dict.fromkeys(("name", "language", "experience", "verbosity"), VALUE),
    "system": {
        **dict.fromkeys(("confirm", "os", "shell", "terminal", "editor", "git_name", "git_email", "tailscale"), VALUE),
        "toolchains": ANY,
    },
    "usage": {"primary": VALUE, "languages": LIST, "frameworks": LIST},
    "providers": {"entries": LIST, "test": VALUE},
    "tiers": {**dict.fromkeys(("main", "strong", "cheap", "fast"), ANY), "context_windows": MAP, "*": MAP},
    "permissions": {"mode": VALUE, "extra_hardline": LIST},
    "integrations": {
        "mcp": LIST,
        "mem0_url": VALUE,
        "skills_roots": LIST,
        "searxng_url": VALUE,
        "test_mem0": VALUE,
        "test_searxng": VALUE,
    },
    "theme": dict.fromkeys(("theme", "focus_mode", "keymap"), VALUE),
    "service": {"install": VALUE},
    "secrets": {"*": VALUE},
    "project_recipes": ANY,
}
#: Without ``providers.entries`` the providers step asks one question per slot (``preset0``, ``key1``, ...).
PER_ENTRY = re.compile(r"(add|preset|base_url|key|env)\d+")
ENTRY_KEYS = ("preset", "name", "base_url", "kind", "api_key_env", "api_key")


def _describe(value: Any) -> str:
    if value is None:
        return "nothing"
    return {dict: "a mapping", list: "a list"}.get(type(value)) or repr(value)


def _unknown(path: str, known: Any, prefix: str = "") -> AnswersError:
    names = [k for k in known if k != "*"]
    near = difflib.get_close_matches(path.removeprefix(prefix), names, n=1)
    hint = f"did you mean '{prefix}{near[0]}'?" if near else f"known: {', '.join(names)}"
    return AnswersError(f"unknown key '{path}' ({hint})")


def _check_entries(entries: list[Any]) -> None:
    from k3code.setup.probe import PRESETS

    for i, entry in enumerate(entries):
        where = f"providers.entries[{i}]"
        if not isinstance(entry, dict):
            raise AnswersError(
                f"{where}: expected a mapping (preset, base_url, api_key_env, ...), got {_describe(entry)}"
            )
        for key in entry:
            if key not in ENTRY_KEYS:
                raise _unknown(f"{where}.{key}", ENTRY_KEYS, f"{where}.")
        if "claude-cli" in (entry.get("preset"), entry.get("kind")):  # Claude Code's own login: no URL, no key
            continue
        if not (entry.get("base_url") or PRESETS.get(str(entry.get("preset")), {}).get("base_url")):
            raise AnswersError(f"{where}: base_url is missing (give one, or a preset: {', '.join(PRESETS)})")
        env = str(entry.get("api_key_env") or "")
        if env and not (env.isascii() and env.isidentifier()):
            raise AnswersError(
                f"{where}.api_key_env: {env!r} is not an environment variable name (letters, digits, _); "
                "give the variable's name, not the key"
            )


def validate_answers(tree: Any) -> None:
    """Raise :class:`AnswersError` unless ``tree`` maps known steps to known fields of the right type."""
    if not isinstance(tree, dict):
        raise AnswersError(f"expected a mapping of steps (about:, providers:, ...), got {_describe(tree)}")
    for step, body in tree.items():
        fields = ANSWER_KEYS.get(step)
        if fields is None:
            raise _unknown(str(step), ANSWER_KEYS)
        if fields == ANY:
            continue
        if not isinstance(body, dict):
            raise AnswersError(f"{step}: expected a mapping of fields, got {_describe(body)}")
        for field, value in body.items():
            kind = fields.get(field) or (VALUE if step == "providers" and PER_ENTRY.fullmatch(str(field)) else None)
            kind = kind or fields.get("*")
            if kind is None:
                raise _unknown(f"{step}.{field}", fields, f"{step}.")
            if kind != ANY and not isinstance(value, _TYPES[kind]):
                raise AnswersError(f"{step}.{field}: expected {kind}, got {_describe(value)}")
    if isinstance(tree.get("providers"), dict) and isinstance(tree["providers"].get("entries"), list):
        _check_entries(tree["providers"]["entries"])


_MISSING: Any = object()


class Prompter:
    interactive = False

    def select(self, key: str, message: str, choices: list[str], default: str | None = None) -> str:
        raise NotImplementedError

    def text(self, key: str, message: str, default: str = "", secret: bool = False) -> str:
        raise NotImplementedError

    def confirm(self, key: str, message: str, default: bool = True) -> bool:
        raise NotImplementedError

    def raw(self, key: str, default: Any = None) -> Any:
        """Structured answer (lists/dicts) from the answers file; None in interactive mode."""
        return default

    def say(self, text: str = "") -> None:
        print(text)


class AnswerPrompter(Prompter):
    """Reads ``<step>.<field>`` from a nested answers mapping; falls back to the default."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers

    @classmethod
    def from_file(cls, path: Path) -> AnswerPrompter:
        """The prompter for an answers file; an unreadable, malformed or invalid file is an AnswersError."""
        try:
            tree = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            raise AnswersError(f"cannot read it ({e})") from e
        except yaml.YAMLError as e:
            mark, problem = getattr(e, "problem_mark", None), getattr(e, "problem", None) or str(e).splitlines()[0]
            raise AnswersError(f"not valid YAML{f' (line {mark.line + 1})' if mark else ''}: {problem}") from e
        validate_answers(tree)
        return cls(tree)

    def raw(self, key: str, default: Any = None) -> Any:
        cur: Any = self.answers
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def select(self, key: str, message: str, choices: list[str], default: str | None = None) -> str:
        """The answer, which must be one of ``choices``; a choice shown as ``name  description`` is answered by name."""
        v = self.raw(key, _MISSING)
        if v is _MISSING:
            return default if default is not None else (choices[0] if choices else "")
        names = [c.split(None, 1)[0] if c.split() else c for c in choices]
        if choices and (v is None or str(v) not in (*choices, *names)):
            raise AnswersError(f"{key}: {_describe(v)} is not one of: {', '.join(names)}")
        return str(v)

    def text(self, key: str, message: str, default: str = "", secret: bool = False) -> str:
        v = self.raw(key, default)
        if isinstance(v, (dict, list)):
            raise AnswersError(f"{key}: expected a single value, got {_describe(v)}")
        return "" if v is None else str(v)

    def confirm(self, key: str, message: str, default: bool = True) -> bool:
        """true or false (a quoted "true"/"false" too); bool("false") is True, so nothing else is guessed at."""
        v = self.raw(key, _MISSING)
        if v is _MISSING:
            return default
        word = v.strip().lower() if isinstance(v, str) else v
        if isinstance(word, bool) or word in ("true", "false"):
            return word if isinstance(word, bool) else word == "true"
        raise AnswersError(f"{key}: {_describe(v)} is not true or false")


class InteractivePrompter(Prompter):
    interactive = True

    def select(self, key: str, message: str, choices: list[str], default: str | None = None) -> str:
        # Long menus (model lists, #122) get a type-ahead filter so a 300-model endpoint does not need 400 DOWN
        # keys: a FuzzyCompleter over the choices, free text accepted (a model id can be typed directly).
        if len(choices) > 8:
            try:
                from prompt_toolkit import prompt as _prompt
                from prompt_toolkit.completion import FuzzyCompleter, WordCompleter
            except ImportError:
                return self._numbered(message, choices, default)
            shown = "\n".join(f"  {i}) {c}" for i, c in enumerate(choices, 1))
            print(f"{message}\n{shown}")
            completer = FuzzyCompleter(WordCompleter(choices, ignore_case=True))
            raw = _prompt(f"{message} [{default or choices[0]}] (number, name or filter text): ",
                          completer=completer, complete_while_typing=True).strip()
            if not raw:
                return default or choices[0]
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                return choices[int(raw) - 1]
            if raw in choices:
                return raw
            # a filter substring: first choice containing it, else the raw text (a custom model id)
            lowered = raw.lower()
            hit = next((c for c in choices if lowered in c.lower()), None)
            return hit if hit is not None else raw
        try:
            from prompt_toolkit.shortcuts import choice
        except ImportError:  # old prompt_toolkit: numbered fallback
            return self._numbered(message, choices, default)
        return str(choice(message=message, options=[(c, c) for c in choices], default=default or choices[0]))

    def _numbered(self, message: str, choices: list[str], default: str | None) -> str:
        for i, c in enumerate(choices, 1):
            print(f"  {i}) {c}")
        raw = input(f"{message} [{default or choices[0]}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        return raw or default or choices[0]

    def text(self, key: str, message: str, default: str = "", secret: bool = False) -> str:
        from prompt_toolkit import prompt

        suffix = f" [{default}]" if default and not secret else ""
        return prompt(f"{message}{suffix}: ", is_password=secret) or default

    def confirm(self, key: str, message: str, default: bool = True) -> bool:
        from prompt_toolkit import prompt

        raw = prompt(f"{message} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not raw else raw.startswith("y")
