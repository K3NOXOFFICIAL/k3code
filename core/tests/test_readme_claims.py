"""README statements that once contradicted the installer, the CHANGELOG or the code (audit of 2026-10-12, #160).

Each test names the source of truth it is held to, so an edit on either side shows up here.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text()


def _bullet(text: str) -> str:
    return next(line for line in README.splitlines() if line.lstrip().startswith("-") and text in line)


def test_the_go_toolchain_comes_from_go_dev_as_the_installer_does_it() -> None:
    installer = (REPO / "install" / "install.sh").read_text()
    assert "https://go.dev/dl/" in installer  # ensure_go downloads the tarball and checks go.dev's sha256
    go = _bullet("Go toolchain")
    assert "from go.dev" in go and "from the Go module proxy" not in go  # the proxy serves only the build's modules


def test_windows_is_not_called_untested_where_powershell_5_1_ran_the_scripts() -> None:
    assert "Untested on a real Windows machine" not in README
    windows = _bullet("**Windows** runs k3code inside")
    assert "PowerShell 5.1" in windows and "stand-in" in windows
    platforms = _bullet("**Platforms.**")
    assert "PowerShell 5.1" in platforms and "stand-in" in platforms


def test_running_24_7_says_what_wsl_changes() -> None:
    section = README.split("## Running 24/7", 1)[1].split("\n---\n", 1)[0]
    assert "WSL" in section and "/etc/wsl.conf" in section and "systemd=true" in section
