"""A config re-read must not retire providers a running turn still holds (#123).

``apply_file_config`` (behind /mcp reload, /output-style --default, /config, /autonomy, /import and reload.env) used
to replace ``config.providers`` with a new list on every call. The next ``_ensure_router`` then closed every live
provider (httpx pools, the warm claude-cli processes) and rebuilt the cooldown store, so a turn of another session
that still held the old router died with "Cannot send a request, as the client has been closed".
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from k3code.config import load_config
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.providers.openai_compat import OpenAICompatProvider

_KEY_ENV = "K3_TEST_RELOAD_KEY"


def _write_config(home: Path, urls: dict[str, str], *, models: str = "m") -> None:
    blocks = "".join(
        f"  - name: {name}\n    kind: openai\n    base_url: {url}\n    api_key_env: {_KEY_ENV}\n"
        f"    models: {{default: {models}}}\n"
        for name, url in urls.items()
    )
    (home / "config.yaml").write_text(f"default_model: default\nproviders:\n{blocks}")


@pytest.fixture
def closed(monkeypatch) -> list[str]:
    """Names of the providers whose ``aclose`` ran."""
    names: list[str] = []

    async def aclose(self) -> None:
        names.append(self.name)

    monkeypatch.setattr(OpenAICompatProvider, "aclose", aclose)
    return names


def _server(tmp_path, monkeypatch, urls: dict[str, str]) -> tuple[GatewayServer, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.setenv(_KEY_ENV, "fake-" + "reload-key")
    _write_config(home, urls)
    srv = GatewayServer(config=load_config(project_dir=proj), store=SessionStore(tmp_path / "s.db"))
    srv._write = lambda s: None
    srv._ensure_router("default")
    return srv, home, proj


async def _settle(srv: GatewayServer) -> None:
    await asyncio.gather(*list(srv._side_tasks))


async def test_a_reload_that_changes_no_provider_keeps_providers_and_cooldowns(tmp_path, monkeypatch, closed):
    srv, _home, proj = _server(tmp_path, monkeypatch, {"a": "http://a", "b": "http://b"})
    providers, cooldowns, router = srv.providers, srv.cooldowns, srv.router
    for _ in range(3):  # /mcp reload, /output-style --default, /config ...
        srv.apply_file_config(proj)
        srv._ensure_router("default")
    await _settle(srv)
    assert srv.providers is providers and srv.cooldowns is cooldowns and srv.router is router
    assert closed == []
    await srv.close()


async def test_editing_one_provider_rebuilds_only_that_block(tmp_path, monkeypatch, closed):
    srv, home, proj = _server(tmp_path, monkeypatch, {"a": "http://a", "b": "http://b"})
    old_a, old_b = srv.providers
    cooldowns = srv.cooldowns
    _write_config(home, {"a": "http://a", "b": "http://b2"})
    srv.apply_file_config(proj)
    srv._ensure_router("default")
    await _settle(srv)
    assert srv.providers[0] is old_a and srv.providers[1] is not old_b
    assert srv.providers[1].base_url == "http://b2"
    assert srv.cooldowns is cooldowns
    assert closed == ["b"]  # only the replaced block is closed
    await srv.close()


async def test_a_model_list_edit_keeps_every_provider(tmp_path, monkeypatch, closed):
    srv, home, proj = _server(tmp_path, monkeypatch, {"a": "http://a"})
    providers = list(srv.providers)
    router = srv.router
    _write_config(home, {"a": "http://a"}, models="m2")  # providers hold no model list: only the routers change
    srv.apply_file_config(proj)
    srv._ensure_router("default")
    await _settle(srv)
    assert srv.providers == providers and srv.router is not router
    assert closed == []
    await srv.close()


async def test_a_retired_provider_stays_open_while_a_turn_may_hold_it(tmp_path, monkeypatch, closed):
    srv, home, proj = _server(tmp_path, monkeypatch, {"a": "http://a", "b": "http://b"})
    srv.PROVIDER_DRAIN_POLL_S = 0.01
    stored = srv.store.create(title="t", model="default", cwd=str(proj))
    live = srv.live_for(stored)
    release = asyncio.Event()
    live.turn_task = asyncio.ensure_future(release.wait())  # a turn of another session is mid-flight
    _write_config(home, {"a": "http://a"})  # block b is removed
    srv.apply_file_config(proj)
    srv._ensure_router("default")
    await asyncio.sleep(0.2)
    assert closed == []  # the turn may still call it
    release.set()
    await live.turn_task
    await _settle(srv)
    assert closed == ["b"]
    await srv.close()


async def test_close_closes_providers_still_waiting_to_retire(tmp_path, monkeypatch, closed):
    srv, home, proj = _server(tmp_path, monkeypatch, {"a": "http://a", "b": "http://b"})
    srv.PROVIDER_DRAIN_POLL_S = 0.01
    stored = srv.store.create(title="t", model="default", cwd=str(proj))
    live = srv.live_for(stored)
    live.turn_task = asyncio.ensure_future(asyncio.Event().wait())
    _write_config(home, {"a": "http://a"})
    srv.apply_file_config(proj)
    srv._ensure_router("default")
    await srv.close()  # the daemon stops with the turn still "running": nothing is left open
    assert sorted(closed) == ["a", "b"]  # each exactly once, though the drain task is still queued
    await _settle(srv)
    assert sorted(closed) == ["a", "b"]
    with contextlib.suppress(asyncio.CancelledError):
        await live.turn_task  # close() cancelled it
