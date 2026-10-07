"""Pytest configuration and fixtures."""

from __future__ import annotations

import httpx
import pytest
import respx


@pytest.fixture
def mock_transport():
    """Provides a respx router for mocking HTTP calls."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def httpx_mock():
    """Alternative: httpx.MockTransport for lower-level control."""
    return httpx.MockTransport


@pytest.fixture(autouse=True)
def _isolated_k3code_home(tmp_path_factory, monkeypatch):
    """Never let tests write decisions/sessions into the real ~/.k3code."""
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path_factory.mktemp("k3home")))
