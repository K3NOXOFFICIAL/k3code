#!/usr/bin/env python3
"""Local TCP proxy that can be switched between up and down without root.

Usage: flaky_proxy.py --listen 127.0.0.1:18080 --upstream <omniroute-host>:20128 --mode-file /tmp/mode

The mode file holds one word, re-read on every connection and every second:
  up    forward traffic to the upstream
  down  stop listening (connection refused); live connections are cut on next data
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


def read_mode(path: Path) -> str:
    try:
        return path.read_text().strip() or "up"
    except OSError:
        return "up"


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter, mode_file: Path) -> None:
    try:
        while data := await r.read(65536):
            if read_mode(mode_file) == "down":
                break
            w.write(data)
            await w.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        w.close()


async def handle(
    cr: asyncio.StreamReader, cw: asyncio.StreamWriter, upstream: tuple[str, int], mode_file: Path
) -> None:
    if read_mode(mode_file) == "down":
        cw.close()
        return
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(*upstream), 5)
    except (OSError, TimeoutError):
        cw.close()
        return
    await asyncio.gather(pipe(cr, uw, mode_file), pipe(ur, cw, mode_file))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:18080")
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--mode-file", required=True, type=Path)
    a = ap.parse_args()
    lh, lp = a.listen.rsplit(":", 1)
    uh, up = a.upstream.rsplit(":", 1)
    a.mode_file.write_text("up")

    async def serve() -> asyncio.Server:
        return await asyncio.start_server(lambda r, w: handle(r, w, (uh, int(up)), a.mode_file), lh, int(lp))

    # "down" really stops listening (connection refused), so TCP probes fail too.
    srv: asyncio.Server | None = await serve()
    while True:
        await asyncio.sleep(0.2)
        want_up = read_mode(a.mode_file) != "down"
        if want_up and srv is None:
            srv = await serve()
        elif not want_up and srv is not None:
            srv.close()
            srv = None


if __name__ == "__main__":
    asyncio.run(main())
