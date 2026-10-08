"""`python -m k3code.cli` must expose every command the click group registers (the module guard runs last)."""

from __future__ import annotations

import re
import subprocess
import sys

from k3code.cli import cli


def _help_command_names(help_text: str) -> set[str]:
    """Command names from the `Commands:` section; wrapped help lines are indented past column 2 and skipped."""
    names: set[str] = set()
    in_section = False
    for line in help_text.splitlines():
        if line == "Commands:":
            in_section = True
            continue
        if in_section:
            m = re.match(r"^  (\S+)", line)
            if m:
                names.add(m.group(1))
    return names


def test_python_m_entrypoint_lists_every_registered_command() -> None:
    registered = set(cli.commands)
    assert {"schedule", "slash"} <= registered

    result = subprocess.run(
        [sys.executable, "-m", "k3code.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    missing = registered - _help_command_names(result.stdout)
    assert not missing, f"missing from `python -m k3code.cli --help`: {sorted(missing)}"
