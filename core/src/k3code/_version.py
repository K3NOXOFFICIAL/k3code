"""Single source of the version: the repo-root ``VERSION`` file (dev checkout) or installed metadata."""

from __future__ import annotations

import sys
from functools import cache
from pathlib import Path


def _read() -> str:
    core = Path(__file__).resolve().parents[2]  # <repo>/core in a checkout
    f = core.parent / "VERSION"
    if (core / "pyproject.toml").is_file() and f.is_file():
        return f.read_text().strip()
    # An installed copy: read the METADATA next to the package. importlib.metadata (email parser included) is
    # ~30 ms of the ~45 ms import, paid on every `k3code --version`; it stays as the fallback.
    for dist_info in sorted(Path(__file__).resolve().parents[1].glob("k3code-*.dist-info")):
        try:
            with (dist_info / "METADATA").open(encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("Version:"):
                        return line.partition(":")[2].strip()
                    if not line.strip():
                        break  # end of the headers
        except OSError:
            continue
    from importlib import metadata

    try:
        return metadata.version("k3code")
    except metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _read()


@cache
def _recorded(prefix: str) -> str | None:
    try:
        return (Path(prefix).parent / ".complete").read_text().strip() or None
    except OSError:
        return None


def recorded_build() -> str | None:
    """The version name the installer recorded for the venv this process runs in, else None (a dev checkout).

    The installer and ``k3code update`` build each version in ``<data>/versions/<name>`` with its venv inside and
    write the name to ``<name>/.complete`` last; that name is how ``k3code update`` tells installs apart. A daemon
    starts through ``<data>/current``, and ``sys.prefix`` keeps that path: read later, the file would be the one of
    whatever ``current`` names by then, not of the code this process loaded. So the first read is kept (and made at
    import, below).
    """
    return _recorded(sys.prefix)


def build_version() -> str:
    """The running install's version name (``0.1.0-src.<sha>``), else ``__version__`` (a dev checkout has no record)."""
    return recorded_build() or __version__


recorded_build()  # at import, while `current` still names the version that is being loaded
