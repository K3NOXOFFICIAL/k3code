"""/memory mem0 searches with the identifiers store_mem0 writes with (issue #162)."""

import json

import httpx
import pytest

from k3code.commands.memory_cmd import mem0_search
from k3code.config import Mem0Config, Settings
from k3code.learning import distiller


def _written_body(cfg: Settings) -> dict:
    sent: list[dict] = []
    prefs = [distiller.Preference("runs the tests first", 0.9, 5, "k")]
    distiller.store_mem0(cfg, prefs, lambda url, body, hdrs: sent.append(body))
    assert len(sent) == 1
    return sent[0]


async def _searched_body(cfg: Settings) -> dict:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"results": [{"memory": "runs the tests first"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await mem0_search(cfg, "tests", client=client)
    assert hits == ["runs the tests first"]
    assert seen[0].url.path == "/search"
    return json.loads(seen[0].content)


@pytest.mark.parametrize("user_id", ["", "keno"])
async def test_search_sends_the_identifiers_the_write_sends(user_id):
    cfg = Settings(mem0=Mem0Config(url="http://mem0.test", agent_id="agent-7", user_id=user_id))
    written, searched = _written_body(cfg), await _searched_body(cfg)
    ids = ("agent_id", "user_id", "run_id")
    assert {k: searched.get(k) for k in ids} == {k: written.get(k) for k in ids}
    assert searched["agent_id"] == "agent-7"


async def test_default_config_search_names_an_owner():
    """Upstream mem0 rejects a search that names no user_id, agent_id or run_id."""
    searched = await _searched_body(Settings(mem0=Mem0Config(url="http://mem0.test")))
    assert searched["agent_id"] == "k3code"
    assert searched["query"] == "tests"
