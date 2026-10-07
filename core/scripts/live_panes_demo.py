"""Pane program: a real GatewayServer (fake provider) with the panes tap on its stdio frames, running one turn.

With LIVE_APPROVAL=1 it then asks for an approval. It ends by sleeping 10 minutes so the pane stays inspectable.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
from pathlib import Path

from k3code.integrations.panes import PaneLink
from m1cmd_helpers import make_server


async def main():
    tmp = Path(os.environ["LIVE_TMP"])
    import pytest

    mp = pytest.MonkeyPatch()
    server, prov = make_server(tmp, mp, replies=["hello from the fake provider"])
    server.panes = PaneLink.from_env(server._pane_inject)
    assert server.panes is not None, "no TUIOS env"
    frames = server._frames
    server._write = lambda line: (frames.append(line), server.panes.on_server_line(line))

    async def rpc(method, params):
        await server._handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))

    await rpc("session.create", {"cwd": str(tmp)})
    time.sleep(3)  # let the harness see idle first
    slow = prov.stream

    async def slow_stream(*a, **k):
        await asyncio.sleep(4)
        async for ev in slow(*a, **k):
            yield ev

    prov.stream = slow_stream
    await rpc("prompt.submit", {"text": "say hi"})
    await asyncio.wait_for(server.session.turn_task, 30)
    time.sleep(1)
    server.panes.reporter.flush()
    print("turn done; state =", server.session.state, flush=True)
    if os.environ.get("LIVE_APPROVAL"):  # hold an approval in the tuios Inbox and wait for the answer
        approve = await server._approval_callback_for(server.session)
        res = await approve("bash", {"command": "rm -rf build"})
        print("approval resolved:", res.choice, flush=True)
    time.sleep(600)


asyncio.run(main())
