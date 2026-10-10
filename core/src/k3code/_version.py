"""Single source of the version: the repo-root ``VERSION`` file (dev checkout) or installed metadata."""

from __future__ import annotations

import sys
from pathlib import Path


def _read() -> str:
    core = Path(__file__).resolve().parents[2]  # <repo>/core in a checkout
    f = core.parent / "VERSION"
    if (core / "pyproject.toml").is_file() and f.is_file():
        return f.read_text().strip()
    # Lazy: only an installed copy (no checkout files) needs importlib.metadata.
    from importlib import metadata

    try:
        return metadata.version("k3code")
    except metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _read()


def build_version() -> str:
    """The running install's version name (``0.1.0-src.<sha>``), else ``__version__``.

    The installer and ``k3code update`` build each version in ``<data>/versions/<name>`` with its venv inside and
    write the name to ``<name>/.complete`` last; that name is how ``k3code update`` tells installs apart. A dev
    checkout's venv has no ``.complete`` next to it, so it shows the plain version.
    """
    try:
        name = (Path(sys.prefix).parent / ".complete").read_text().strip()
    except OSError:
        return __version__
    return name or __version__
