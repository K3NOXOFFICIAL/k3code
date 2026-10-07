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
