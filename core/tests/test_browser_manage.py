"""browser.manage over the gateway: status starts nothing, connect never attaches unless browser.cdp_url is set,
and the method matches the generated TypeScript contract."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from k3code.gateway import server as srv
from k3code.research import browser as b
from test_autonomy_gateway import call, make

TUI_DEFAULT = "http://127.0.0.1:9222"  # what tui/src/app/slash/commands/ops.ts sends for /browser connect


async def error_of(server, method: str, params: dict) -> dict:
    await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": 9, "method": method, "params": params}))
    return next(
        json.loads(x)["error"] for x in server._frames if json.loads(x).get("id") == 9 and "error" in json.loads(x)
    )


def _fake_attach(server, attached: list[str]):
    async def attach(endpoint: str) -> None:
        attached.append(endpoint)
        server.browser._cdp = SimpleNamespace(contexts=[], close=lambda: None)

    return attach


async def test_status_with_no_browser_running_starts_nothing(tmp_path, monkeypatch):
    def must_not_start():
        raise AssertionError("browser.manage status must not start a browser")

    monkeypatch.setattr(b, "playwright_module", must_not_start)
    server = make(tmp_path, monkeypatch, [])
    assert await call(server, "browser.manage", {"action": "status", "session_id": None}) == {
        "connected": False,
        "url": None,
        "messages": [],
    }


async def test_connect_with_default_settings_never_attaches_to_an_existing_browser(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [])
    attached: list[str] = []
    monkeypatch.setattr(server.browser, "attach_cdp", _fake_attach(server, attached))
    r = await call(server, "browser.manage", {"action": "connect", "url": TUI_DEFAULT, "session_id": None})
    assert r["connected"] is False and attached == []
    assert "off by default" in r["messages"][0] and "browser.cdp_url" in r["messages"][0]


async def test_connect_attaches_only_to_the_configured_cdp_url(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [], browser={"cdp_url": "http://127.0.0.1:9333"})
    attached: list[str] = []
    monkeypatch.setattr(server.browser, "attach_cdp", _fake_attach(server, attached))
    refused = await call(server, "browser.manage", {"action": "connect", "url": TUI_DEFAULT})
    assert refused["connected"] is False and attached == []
    assert "not the configured" in refused["messages"][0]

    done = await call(server, "browser.manage", {"action": "connect", "url": "http://127.0.0.1:9333"})
    assert (
        done["connected"] is True and done["url"] == "http://127.0.0.1:9333" and attached == ["http://127.0.0.1:9333"]
    )
    assert (await call(server, "browser.manage", {"action": "status"}))["connected"] is True

    gone = await call(server, "browser.manage", {"action": "disconnect"})
    assert gone == {"connected": False, "url": None, "messages": ["browser disconnected"]}


async def test_connect_without_playwright_says_so_and_stays_disconnected(tmp_path, monkeypatch):
    def missing():
        raise b.BrowserUnavailable(b.INSTALL_HINT)

    monkeypatch.setattr(b, "playwright_module", missing)
    server = make(tmp_path, monkeypatch, [], browser={"cdp_url": "http://127.0.0.1:9333"})
    r = await call(server, "browser.manage", {"action": "connect", "url": "http://127.0.0.1:9333"})
    assert r["connected"] is False and "Playwright" in r["messages"][0]


async def test_an_unknown_action_is_invalid_params(tmp_path, monkeypatch):
    server = make(tmp_path, monkeypatch, [])
    err = await error_of(server, "browser.manage", {"action": "launch"})
    assert err["code"] == -32602 and "launch" in err["message"]


def test_browser_manage_matches_the_generated_typescript_contract():
    ts = Path(__file__).resolve().parents[2] / "tui" / "shared" / "gateway-contract.generated.ts"
    if not ts.is_file():
        pytest.skip("tui/shared/gateway-contract.generated.ts is not in this checkout")
    text = ts.read_text(encoding="utf-8")
    assert "'browser.manage': { params: BrowserManageParams; result: BrowserManageResult }" in text
    assert "browser.manage" in srv._HANDLERS
    assert "export type BrowserAction = 'status' | 'connect' | 'disconnect'" in text
    for iface, fields in (
        ("BrowserManageParams", ("action", "url", "session_id", "profile")),
        ("BrowserManageResult", ("connected", "url", "messages")),
    ):
        body = re.search(rf"export interface {iface} \{{(.*?)\n\}}", text, re.S)
        assert body, iface
        for field in fields:
            assert re.search(rf"^\s*{field}\??:", body.group(1), re.M), (iface, field)
