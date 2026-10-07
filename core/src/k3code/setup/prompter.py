"""Prompt back-ends: interactive (prompt_toolkit arrow keys) and answers-file (non-interactive)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


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
        return cls(yaml.safe_load(path.read_text()) or {})

    def raw(self, key: str, default: Any = None) -> Any:
        cur: Any = self.answers
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def select(self, key: str, message: str, choices: list[str], default: str | None = None) -> str:
        v = self.raw(key, default if default is not None else (choices[0] if choices else ""))
        return str(v)

    def text(self, key: str, message: str, default: str = "", secret: bool = False) -> str:
        v = self.raw(key, default)
        return "" if v is None else str(v)

    def confirm(self, key: str, message: str, default: bool = True) -> bool:
        return bool(self.raw(key, default))


class InteractivePrompter(Prompter):
    interactive = True

    def select(self, key: str, message: str, choices: list[str], default: str | None = None) -> str:
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
