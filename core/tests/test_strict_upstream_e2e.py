"""End to end through the real OpenAI-compatible provider into an upstream that enforces the wire rules.

The scripted fake provider accepts anything, which hid that turn 2 of a tool-using session was malformed (orphan
``tool`` messages). ``scripts/chaos/fake_upstream.py`` now answers HTTP 400 for such requests like the real APIs.
"""

from __future__ import annotations

import importlib.util
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from k3code.config import ProviderEntry, Settings
from k3code.gateway.server import GatewayServer
from k3code.gateway.sessions import SessionStore

UPSTREAM = Path(__file__).resolve().parents[2] / "scripts" / "chaos" / "fake_upstream.py"


@pytest.fixture
def upstream():
    spec = importlib.util.spec_from_file_location("fake_upstream_strict", UPSTREAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


async def test_second_turn_of_a_tool_session_is_accepted_by_a_strict_upstream(tmp_path, monkeypatch, upstream):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STRICT_KEY", "x")
    monkeypatch.delenv("K3CODE_FAKE_PROVIDER", raising=False)
    prov = ProviderEntry(name="strict", kind="openai", base_url=upstream, api_key_env="STRICT_KEY", api_key="x",
                         models={"default": "m"})
    srv = GatewayServer(config=Settings(providers=[prov], default_model="default", permission_mode="yolo"),
                        store=SessionStore(tmp_path / "s.db"))
    srv._write = lambda s: None
    stored = srv.store.create(title="t", model="default", cwd=str(tmp_path))
    live = srv.live_for(stored)
    try:
        status1, text1 = await srv._run_turn(live, "RUN[echo hi] run it")
        assert status1 == "done", (text1, live.last_error, str(live.last_exc))
        assert "hi" in text1  # the tool really ran and the final answer reports its result
        assert any(m.get("tool_calls") for m in live.stored.messages)  # turn 1 used a tool
        for n in range(3):  # turns 2..4 carry the earlier tool call + result back to the provider
            status, text = await srv._run_turn(live, f"and again {n}")
            assert status == "done", (n, text, live.last_error)
    finally:
        await srv.close()
