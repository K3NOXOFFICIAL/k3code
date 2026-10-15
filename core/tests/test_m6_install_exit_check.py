"""The M6 fresh-container exit check installs as root, so it must pass the installer's --allow-root switch."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "exit" / "m6_install.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="scripts/exit is not part of the release export")


@pytest.fixture
def m6(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("m6_install_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


def test_container_install_passes_allow_root(m6):
    lines = [ln for ln in m6.INNER.splitlines() if re.match(r"\s*sh install/install\.sh\b", ln)]
    assert len(lines) == 1
    assert "--allow-root" in lines[0].replace(";", " ").split()
    # the flag has to be one the installer really parses, or it would die on an unknown option
    assert "--allow-root)" in (REPO / "install" / "install.sh").read_text()


def test_standalone_run_sets_exit_rows(m6, monkeypatch, tmp_path):
    monkeypatch.delenv("EXIT_ROWS", raising=False)
    monkeypatch.setattr(m6, "LOGS", tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    m6.default_exit_rows()
    m6.emit("M6", "x", "how", "FAIL", "evidence kept")
    rows = (tmp_path / "m6_rows.jsonl").read_text()
    assert "evidence kept" in rows


def test_run_all_exit_rows_wins(m6, monkeypatch, tmp_path):
    monkeypatch.setenv("EXIT_ROWS", str(tmp_path / "given.jsonl"))
    monkeypatch.setattr(m6, "LOGS", tmp_path / "logs")
    m6.default_exit_rows()
    assert m6.os.environ["EXIT_ROWS"] == str(tmp_path / "given.jsonl")
    assert not (tmp_path / "m6_rows.jsonl").exists()
