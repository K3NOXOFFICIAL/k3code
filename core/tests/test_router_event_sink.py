"""Issue #148: an event sink that raises (the gateway's status update to a client that has gone) must not abort a
failover walk."""

from __future__ import annotations

from k3code.providers.fake import FakeProvider
from k3code.providers.types import Message
from k3code.router import Router, build_chain


async def test_a_raising_event_sink_does_not_abort_the_failover():
    def sink(event):
        raise RuntimeError("client gone")

    primary = FakeProvider(name="primary", steps=[{"type": "error", "status_code": 500, "message": "on fire"}])
    secondary = FakeProvider(name="secondary", steps=[{"type": "text", "text": "ok"}])
    chain = build_chain([primary, secondary], [["m1"], ["m2"]])
    router = Router(chain, max_retries=0, base_delay=0.01, max_delay=0.01, on_event=sink)

    result = await router.complete([Message(role="user", content="hi")], [])

    assert result.content == "ok"
