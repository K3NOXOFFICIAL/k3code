"""Fixtures for tests that need more of the machine than a container gives.

``hide_root``: install.sh refuses to run as root, and its tests are about everything else: they run as an ordinary
user on a developer's machine but as root in a container. It puts an ``id`` that reports an ordinary user first on
PATH for the test; a test about root brings its own ``id`` stub (put in front of this one) or runs install.sh with
--allow-root.

``sandbox_optional``: unattended commands run in bubblewrap and are refused where it cannot start (user namespaces
blocked, as in many containers). The fan-out, ultra and goal-check tests are about the pipeline, not the sandbox, so
where bwrap is unusable the gate is stubbed and their commands run directly in the test's temporary directory. Where
bwrap works nothing is stubbed and they run sandboxed, as they always did."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from k3code.reliability import sandbox

_ID = '#!/bin/sh\ncase "$1" in -u) echo 1000 ;; -un) echo nobody ;; *) echo "uid=1000(nobody)" ;; esac\n'


@pytest.fixture
def hide_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.geteuid() != 0:
        return
    stub_dir = tmp_path_factory.mktemp("nonroot-id")
    stub = Path(stub_dir) / "id"
    stub.write_text(_ID)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def sandbox_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("bwrap") and sandbox.usable():
        return
    monkeypatch.setattr(sandbox, "should_sandbox", lambda *a, **k: False)
    monkeypatch.setattr(sandbox, "unattended_prefix", lambda *a, **k: [])
