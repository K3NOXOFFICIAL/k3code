#!/usr/bin/env python3
"""Verify VENDOR.toml entries: files exist, licenses allowed, no banned projects."""

from __future__ import annotations

import hashlib
import sys
import tomllib
from pathlib import Path

ALLOWED_LICENSES = {"MIT", "Apache-2.0"}
BANNED_PROJECTS = {"open-claudecode", "claude-code-leak", "ante-binary"}


def check_vendor() -> int:
    repo_root = Path(__file__).parent.parent
    vendor_path = repo_root / "VENDOR.toml"
    if not vendor_path.is_file():
        print("ERROR: VENDOR.toml not found")
        return 1

    with vendor_path.open("rb") as f:
        data = tomllib.load(f)

    errors = 0
    for i, entry in enumerate(data.get("file", [])):
        local_path = repo_root / entry["local_path"]
        project = entry["project"]
        license_ = entry["license"]
        upstream_path = entry["upstream_path"]
        commit = entry["commit"]

        # Check file exists
        if not local_path.is_file():
            print(f"ERROR: [{i}] Missing file: {local_path}")
            errors += 1
            continue

        # Check license
        if license_ not in ALLOWED_LICENSES:
            print(f"ERROR: [{i}] Disallowed license '{license_}' for {project}")
            errors += 1

        # Check banned projects
        if project.lower() in BANNED_PROJECTS:
            print(f"ERROR: [{i}] Banned project: {project}")
            errors += 1

        # Verify sha256 if provided
        sha256 = entry.get("sha256_upstream")
        # A file marked modified = true is expected to differ from its upstream hash.
        if sha256 and not entry.get("modified", False):
            actual = hashlib.sha256(local_path.read_bytes()).hexdigest()
            if actual != sha256:
                print(f"WARN: [{i}] SHA256 mismatch for {local_path} (not marked modified)")
                print(f"  expected: {sha256}")
                print(f"  actual:   {actual}")

        print(f"OK: [{i}] {project}@{commit[:8]} {upstream_path} -> {local_path} ({license_})")

    if errors:
        print(f"\n{errors} error(s)")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(check_vendor())
