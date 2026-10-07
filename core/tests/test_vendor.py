"""Vendor check script tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_vendor_check_passes():
    repo_root = Path(__file__).parent.parent.parent
    result = subprocess.run(
        [sys.executable, "scripts/vendor_check.py"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"vendor_check failed: {result.stdout}\n{result.stderr}"
    assert "All checks passed" in result.stdout


def test_vendor_check_detects_missing():
    # Temporarily rename a file to test detection
    repo_root = Path(__file__).parent.parent.parent
    vendor_file = repo_root / "core/src/k3code/providers/retry_utils.py"
    backup = repo_root / "core/src/k3code/providers/retry_utils.py.bak"
    vendor_file.rename(backup)
    try:
        result = subprocess.run(
            [sys.executable, "scripts/vendor_check.py"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "Missing file" in result.stdout
    finally:
        backup.rename(vendor_file)
