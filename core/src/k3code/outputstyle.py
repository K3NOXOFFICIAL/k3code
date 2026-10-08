"""Output styles: presets plus custom ``*.md`` files; the style text is appended to the system prompt."""

from __future__ import annotations

from pathlib import Path

from k3code.paths import home

PRESETS: dict[str, str] = {
    "default": "",
    "concise": (
        "Output style: concise. Answer in as few words as possible. No preamble, no recap of what you did "
        "unless asked; code and commands over prose."
    ),
    "explanatory": (
        "Output style: explanatory. While working, briefly explain the why behind implementation choices and "
        "codebase patterns you notice, so the user learns the reasoning, not just the result."
    ),
    "learning": (
        "Output style: learning. Teach while you work: explain key decisions, and for small, well-scoped "
        "pieces of the task leave a clearly marked TODO(human) for the user to implement themselves, "
        "with enough guidance for them to do it."
    ),
}


def _dirs(cwd: str | Path) -> list[Path]:
    return [home() / "output-styles", Path(cwd) / ".k3code" / "output-styles"]


def custom_styles(cwd: str | Path) -> dict[str, Path]:
    """name → file; the project directory overrides the user directory."""
    out: dict[str, Path] = {}
    for d in _dirs(cwd):
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                out[f.stem] = f
    return out


def available(cwd: str | Path) -> list[str]:
    return [*PRESETS, *sorted(n for n in custom_styles(cwd) if n not in PRESETS)]


def style_text(name: str, cwd: str | Path) -> str | None:
    """Style text for ``name`` ('' for default); None if unknown. Custom files may override presets."""
    custom = custom_styles(cwd)
    if name in custom:
        return custom[name].read_text(encoding="utf-8").strip()
    if name in PRESETS:
        return PRESETS[name]
    return None
