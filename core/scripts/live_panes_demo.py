"""Pane program: a real GatewayServer (fake provider) with the panes tap on its stdio frames, running two turns."""
import asyncio, json, sys, os, time
sys.path.insert(0, "tests")
from pathlib import Path
from k3code.integrations.panes import PaneLink
from m1cmd_helpers import make_server, TextProvider

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
        await server._handle_line(json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}))
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
    time.sleep(600)
asyncio.run(main())
