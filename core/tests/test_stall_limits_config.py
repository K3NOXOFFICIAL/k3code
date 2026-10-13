"""The stall limits (providers/stall.py) are a provider setting, and a server the user runs gets the long one.

A local server prefills a long prompt before its first token; at the default 60 s it was cut off, and the stall retry
then repeated the same prefill."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from k3code.config import ConfigError, ProviderEntry, load_config
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore
from k3code.providers import make_providers, provider_signature
from k3code.providers.stall import REASONING_TIMEOUT, is_local_endpoint
from k3code.providers.types import Message

BLOCK = "providers:\n  - name: p\n    kind: openai\n    base_url: https://x.test/v1\n    api_key_env: K3_STALL_KEY\n"


def _write_user_config(text: str) -> None:
    home = Path(os.environ["K3CODE_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text)


def entry(**kw) -> ProviderEntry:
    return ProviderEntry(
        **{"name": "p", "kind": "openai", "base_url": "https://x.test/v1", "api_key_env": "K3_STALL_KEY", **kw}
    )


def limits_of(**kw) -> tuple[float | None, float | None]:
    provider = make_providers([entry(**kw)])[0]
    return provider._first_event_timeout, provider._idle_timeout


@pytest.mark.parametrize("key", ["first_event_timeout", "idle_timeout"])
@pytest.mark.parametrize("value", ["0", "-5", ".inf", ".nan", "fast"])
def test_a_bad_stall_limit_is_a_config_error_naming_the_key(key, value):
    _write_user_config(f"{BLOCK}    {key}: {value}\n")
    with pytest.raises(ConfigError, match=key):
        load_config()


def test_stall_limits_are_read_from_the_config_and_reach_the_provider(monkeypatch):
    monkeypatch.setenv("K3_STALL_KEY", "k")
    _write_user_config(f"{BLOCK}    first_event_timeout: 240\n    idle_timeout: 90.5\n")
    [cfg] = load_config().providers
    assert (cfg.first_event_timeout, cfg.idle_timeout) == (240, 90.5)
    assert limits_of(first_event_timeout=240, idle_timeout=90.5) == (240, 90.5)
    for kind in ("openai", "anthropic"):
        assert limits_of(kind=kind, first_event_timeout=45) == (45, None)
    _write_user_config(BLOCK)
    [bare] = load_config().providers
    assert (bare.first_event_timeout, bare.idle_timeout) == (None, None)


def test_a_changed_stall_limit_changes_the_provider_signature():
    signatures = {
        json.dumps(provider_signature(entry(**kw)), sort_keys=True)
        for kw in ({}, {"first_event_timeout": 200}, {"idle_timeout": 400})
    }
    assert len(signatures) == 3, "a config reload must rebuild a provider whose limits changed"


async def test_a_reload_that_changes_a_stall_limit_rebuilds_that_provider(tmp_path, monkeypatch):
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("K3CODE_HOME", str(home))
    monkeypatch.setenv("K3_STALL_KEY", "k")

    def write(extra: str) -> None:
        (home / "config.yaml").write_text(f"default_model: default\n{BLOCK}{extra}    models: {{default: m}}\n")

    write("")
    srv = GatewayServer(config=load_config(project_dir=proj), store=SessionStore(tmp_path / "s.db"))
    srv._write = lambda s: None
    srv._ensure_router("default")
    old = srv.providers[0]
    srv.apply_file_config(proj)
    srv._ensure_router("default")
    assert srv.providers[0] is old, "nothing changed: the provider stays"
    write("    first_event_timeout: 200\n")
    srv.apply_file_config(proj)
    srv._ensure_router("default")
    assert srv.providers[0] is not old and srv.providers[0]._first_event_timeout == 200
    await asyncio.gather(*list(srv._side_tasks))
    await srv.close()


LOCAL = [
    ("http://127.0.0.1:11434/v1", True),
    ("http://127.0.0.2:8000/v1", True),
    ("http://localhost:8080/v1", True),
    ("http://LocalHost/v1", True),
    ("http://[::1]:8080/v1", True),
    ("http://studio.local:1234/v1", True),
    ("https://api.openai.com/v1", False),
    ("https://localhost.example.com/v1", False),
    ("https://gateway.internal.example/v1", False),
    ("http://10.0.0.5:8000/v1", False),  # a LAN address says nothing; a keyless entry is the signal there
    ("not a url", False),
    ("", False),
]


@pytest.mark.parametrize(("url", "local"), LOCAL)
def test_a_loopback_or_dot_local_host_is_a_local_endpoint(url, local):
    assert is_local_endpoint(url) is local


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize(("url", "local"), LOCAL[:8])
def test_a_local_server_defaults_to_the_reasoning_limit(kind, url, local):
    longer = REASONING_TIMEOUT if local else None
    assert limits_of(kind=kind, base_url=url) == (longer, longer)


def test_a_keyless_entry_defaults_to_the_long_limit_wherever_it_is():
    assert limits_of(base_url="http://10.0.0.5:11434/v1", api_key_env="") == (REASONING_TIMEOUT, REASONING_TIMEOUT)


def test_a_limit_in_the_config_wins_over_the_local_default():
    local = {"base_url": "http://127.0.0.1:11434/v1", "api_key_env": ""}
    assert limits_of(**local, first_event_timeout=45) == (45, REASONING_TIMEOUT)
    assert limits_of(**local, idle_timeout=30) == (REASONING_TIMEOUT, 30)


async def test_a_slow_prefill_on_a_local_server_is_not_cut_off_where_a_remote_one_is(monkeypatch):
    # the real limits (60 s / 300 s) scaled down: the first token comes after 0.4 s, the default limit is 0.2 s
    monkeypatch.setattr("k3code.providers.stall.FIRST_EVENT_TIMEOUT", 0.2)
    monkeypatch.setattr("k3code.providers.REASONING_TIMEOUT", 2.0)

    class SlowPrefill(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.4)
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowPrefill())

    async def first_reply(**kw) -> str:
        provider = make_providers([entry(api_key="k", **kw)])[0]
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        events = [e async for e in provider.stream([Message(role="user", content="x")], [], "qwen3-coder")]
        return events[-1].message.content

    assert await first_reply(base_url="http://127.0.0.1:11434/v1", api_key_env="") == "ok"
    with pytest.raises(Exception, match="stalled"):
        await first_reply(base_url="https://remote.example/v1")
