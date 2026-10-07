"""Single source of the version: the repo-root ``VERSION`` file (dev checkout) or installed metadata."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path


def _read() -> str:
    core = Path(__file__).resolve().parents[2]  # <repo>/core in a checkout
    f = core.parent / "VERSION"
    if (core / "pyproject.toml").is_file() and f.is_file():
        return f.read_text().strip()
    try:
        return metadata.version("k3code")
    except metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _read()
