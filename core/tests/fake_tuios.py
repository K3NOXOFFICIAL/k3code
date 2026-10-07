"""A fake tuios verb socket: records every request, answers from a table of handlers."""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


class FakeTuios:
    def __init__(self, answers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="k3t"))  # short: unix socket paths are capped near 100 bytes
        self.path = str(self.dir / "t.sock")
        self.answers = answers or {}
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(8)
        self._stop = False
        self._cond = threading.Condition(self._lock)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r") as fh:
            for line in fh:
                req = json.loads(line)
                with self._cond:
                    self.requests.append(req)
                    self._cond.notify_all()
                handler = self.answers.get(req["verb"])
                result = handler(req.get("params") or {}) if handler else {"applied": True}
                try:
                    conn.sendall((json.dumps({"id": req["id"], "result": result}) + "\n").encode())
                except OSError:
                    return

    def verbs(self, verb: str) -> list[dict[str, Any]]:
        with self._lock:
            return [r["params"] for r in self.requests if r["verb"] == verb]

    def wait_for(self, verb: str, n: int = 1, timeout: float = 5.0) -> list[dict[str, Any]]:
        with self._cond:
            self._cond.wait_for(lambda: sum(r["verb"] == verb for r in self.requests) >= n, timeout)
        return self.verbs(verb)

    def env(self, pane: str = "pane-1", session: str = "work") -> dict[str, str]:
        return {"TUIOS_SOCKET": self.path, "TUIOS_PANE_ID": pane, "TUIOS_SESSION": session}

    def close(self) -> None:
        self._stop = True
        self._srv.close()
        shutil.rmtree(self.dir, ignore_errors=True)
