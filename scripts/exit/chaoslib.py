"""Helpers shared by m2_chaos.py and soak.py: temp homes, fake upstream/proxy, a daemon and a JSON-RPC peer."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHAOS = REPO / "scripts" / "chaos"
K3CODE = REPO / "core" / ".venv" / "bin" / "k3code"


def k3code_bin() -> str:
    """The project venv's k3code (so HOME can be a temp dir without re-resolving uv)."""
    if K3CODE.exists():
        return str(K3CODE)
    return shutil.which("k3code") or str(K3CODE)


class Procs:
    """Background processes (own process groups) killed on exit."""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []

    def spawn(
        self, cmd: list[str], *, env: dict | None = None, cwd: Path | str | None = None, log: Path | None = None
    ) -> subprocess.Popen:
        out = open(log, "ab") if log else subprocess.DEVNULL  # noqa: SIM115
        p = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(p)
        return p

    def kill(self, p: subprocess.Popen, sig: int = signal.SIGTERM) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, sig)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)

    def close(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                self.kill(p)


def wait_port(port: int, timeout: float = 15) -> None:
    import socket

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"port {port} never opened")


def start_fake_upstream(procs: Procs, port: int, *args: str) -> subprocess.Popen:
    p = procs.spawn([sys.executable, str(CHAOS / "fake_upstream.py"), str(port), *args])
    wait_port(port)
    return p


def start_proxy(procs: Procs, listen: int, upstream: int, mode_file: Path) -> subprocess.Popen:
    p = procs.spawn(
        [
            sys.executable,
            str(CHAOS / "flaky_proxy.py"),
            "--listen",
            f"127.0.0.1:{listen}",
            "--upstream",
            f"127.0.0.1:{upstream}",
            "--mode-file",
            str(mode_file),
        ]
    )
    wait_port(listen)
    return p


class Peer:
    """JSON-RPC client over the daemon socket; every event is stamped with time.monotonic()."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.r, self.w = reader, writer
        self.events: list[tuple[float, dict]] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._n = 0
        self._task = asyncio.create_task(self._pump())

    @classmethod
    async def connect(cls, sock: Path, timeout: float = 30) -> Peer:
        from k3code.gateway.auth import authenticate  # the daemon refuses privileged RPCs without gateway.auth

        end = time.monotonic() + timeout
        early: list[tuple[float, dict]] = []

        def keep(line: bytes) -> None:  # events sent before the auth reply (gateway.ready, notifications)
            frame = json.loads(line)
            if frame.get("method") == "event":
                early.append((time.monotonic(), frame["params"]))

        while True:
            try:
                r, w = await asyncio.open_unix_connection(str(sock))
                early.clear()
                await authenticate(r, w, sock, keep)  # before the pump starts: it would steal the reply
                peer = cls(r, w)
                peer.events[:0] = early
                return peer
            except OSError:
                if time.monotonic() > end:
                    raise
                await asyncio.sleep(0.2)

    async def _pump(self) -> None:
        try:
            while line := await self.r.readline():
                frame = json.loads(line)
                if frame.get("method") == "event":
                    self.events.append((time.monotonic(), frame["params"]))
                elif (fut := self._pending.pop(frame.get("id"), None)) is not None:
                    fut.set_result(frame)
        except (ConnectionError, ValueError):
            pass

    async def call(self, method: str, timeout: float = 30, **params):
        self._n += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._n] = fut
        self.w.write(
            (json.dumps({"jsonrpc": "2.0", "id": self._n, "method": method, "params": params}) + "\n").encode()
        )
        await self.w.drain()
        return await asyncio.wait_for(fut, timeout)

    def find(self, etype: str, after: float = 0.0) -> tuple[float, dict] | None:
        for ts, e in self.events:
            if e.get("type") == etype and ts >= after:
                return ts, e
        return None

    async def wait_for(self, etype: str, timeout: float, after: float = 0.0) -> tuple[float, dict] | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if (hit := self.find(etype, after)) is not None:
                return hit
            await asyncio.sleep(0.05)
        return None

    def count(self, etype: str) -> int:
        return sum(1 for _, e in self.events if e.get("type") == etype)

    def close(self) -> None:
        self._task.cancel()
        self.w.close()


class Daemon:
    """`k3code daemon` in a temp HOME/K3CODE_HOME, on the fake provider or on a real http upstream."""

    def __init__(self, procs: Procs, root: Path | None = None, extra_env: dict | None = None) -> None:
        self.procs = procs
        self.root = root or Path(tempfile.mkdtemp(prefix="k3exit."))
        self.home = self.root / "home"
        self.proj = self.root / "proj"
        self.home.mkdir(parents=True, exist_ok=True)
        self.proj.mkdir(parents=True, exist_ok=True)
        self.sock = self.home / "run" / "gateway.sock"
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "K3CODE_FAKE_PROVIDER",
                "NOTIFY_SOCKET",
                "K3CODE_GATEWAY_SOCKET",
                "HERMES_TUI_GATEWAY_URL",
                "OMNIROUTE_API_KEY",
            )
        }
        self.env.update(HOME=str(self.root), K3CODE_HOME=str(self.home), **(extra_env or {}))
        self.proc: subprocess.Popen | None = None
        self.log = self.root / "daemon.log"

    def write_config(self, yaml_text: str) -> None:
        (self.home / "config.yaml").write_text(yaml_text)

    def start(self) -> None:
        self.proc = self.procs.spawn([k3code_bin(), "daemon"], env=self.env, cwd=self.proj, log=self.log)

    async def peer(self) -> Peer:
        return await Peer.connect(self.sock)

    def rss_kb(self) -> int:
        """Resident memory of the daemon process, in kB (0 when gone)."""
        assert self.proc is not None
        try:
            for line in Path(f"/proc/{self.proc.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except OSError:
            pass
        return 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None
