"""M0 minimal permission policy."""

from __future__ import annotations

from enum import StrEnum


class PermissionMode(StrEnum):
    ASK = "ask"
    AUTO_EDIT = "auto-edit"
    YOLO = "yolo"


def check_permission(mode: PermissionMode, tool_name: str, *, headless: bool = False) -> tuple[bool, str | None]:
    """Check if a tool call is allowed under the current permission mode.

    Returns (allowed, error_message). If not allowed and headless with ask mode,
    returns a clear denial message instead of prompting.
    """
    # Tools with side effects
    edit_tools = {"write", "edit"}
    side_effect_tools = edit_tools | {"bash"}
    pure_tools = {"read", "grep", "glob", "todo"}

    if tool_name in pure_tools:
        return True, None

    if tool_name not in side_effect_tools:
        return True, None  # unknown tool, let it through

    denial = f"Permission denied: {tool_name} requires approval (mode={mode.value})"

    if headless and mode == PermissionMode.ASK:
        return False, denial

    if mode == PermissionMode.YOLO:
        return True, None

    if mode == PermissionMode.AUTO_EDIT:
        # Edits are allowed outright; bash still needs approval.
        return (tool_name in edit_tools), None if tool_name in edit_tools else denial

    # mode == ASK (interactive) - in M0 we don't actually prompt, just deny in headless
    # The interactive REPL will handle prompting separately
    if headless:
        return False, denial
    return True, None  # interactive: would prompt, but M0 doesn't implement prompting yet
