"""TUI display settings (/indicator, /statusbar, /battery, /pet, ...) go through config.set and survive a restart."""

from __future__ import annotations

import pytest

from k3code.confio import read_yaml
from k3code.paths import user_config_path
from test_permissions_gateway import call, make_server


async def test_indicator_set_get_and_saved(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    r = await call(server, "config.set", {"key": "indicator", "value": "ascii"})
    assert r == {"ok": True, "key": "indicator", "value": "ascii"}
    assert (await call(server, "config.get", {"key": "indicator"}))["value"] == "ascii"
    full = await call(server, "config.get", {"key": "full"})
    assert full["config"]["display"]["tui_status_indicator"] == "ascii"
    assert read_yaml(user_config_path())["display"]["tui_status_indicator"] == "ascii"


@pytest.mark.parametrize(
    ("key", "value", "field", "stored", "wire"),
    [
        ("battery", "on", "battery", True, "on"),
        ("mouse", False, "mouse_tracking", False, "off"),
        ("density", "off", "tui_compact", False, "off"),
        ("statusbar", "top", "tui_statusbar", "top", "top"),
        ("theme", "dark", "tui_theme", "dark", "dark"),
        ("pet", "off", "pet", "off", "off"),
    ],
)
async def test_tui_keys_map_to_display_fields(tmp_path, monkeypatch, key, value, field, stored, wire):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    r = await call(server, "config.set", {"key": key, "value": value})
    assert r["value"] == wire
    assert server.config.display.model_dump()[field] == stored


async def test_details_section_override(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    await call(server, "config.set", {"key": "details_mode.tools", "value": "hidden"})
    assert server.config.display.model_dump()["sections"] == {"tools": "hidden"}
    await call(server, "config.set", {"key": "details_mode.tools", "value": ""})
    assert server.config.display.model_dump()["sections"] == {}


async def test_bad_bool_is_an_error(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, ["ok"], monkeypatch)
    with pytest.raises(Exception):  # noqa: B017 - the helper raises on a JSON-RPC error reply
        await call(server, "config.set", {"key": "battery", "value": "maybe"})
