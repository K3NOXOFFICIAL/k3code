"""The grep fallback's walk (the daemon's side of the search, see tools._grep_fallback) costs few file-system calls per
file: each file used to cost about 8 realpath calls (the walk's own check, then sensitive_path, secret_store and the
withholding rule each resolving the file and the secret dirs again) and the ~100 lstat calls they make, which held a
100k-file repository to about 6k files a second. The worker refuses any link on the way to a file (an O_NOFOLLOW
chain), so the walk resolves the searched directory and each directory it enters once, and judges a file by its name
below them."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

import k3code.tools as tools
from k3code.permissions.engine import search_withheld
from k3code.tools import grep_worker

DIRS = 20
FILES_PER_DIR = 100
#: The budget per file, all four calls together: the walk cost about 61 here before (8 realpath, 53 lstat).
MAX_CALLS_PER_FILE = 2


class _Sink(io.BytesIO):
    """The worker's stdin: kept readable after the feeder closes it."""

    def close(self) -> None:
        pass


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))  # no git, no rg: the fallback's own os.walk
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / "home").mkdir()
    proj = tmp_path / "proj"
    for d in range(DIRS):
        sub = proj / f"pkg{d:02d}"
        sub.mkdir(parents=True)
        for f in range(FILES_PER_DIR):
            (sub / f"mod{f:03d}.py").write_text("needle\n" if f % 10 == 0 else "hay\n")
    return proj


@pytest.mark.parametrize("inside", [True, False], ids=["inside-roots", "outside-roots"])
def test_the_walk_costs_a_few_file_system_calls_per_file(tree, tmp_path, monkeypatch, inside) -> None:
    roots = [str(tree)] if inside else [str(tmp_path / "home")]
    counts: Counter[str] = Counter()
    for mod, name in ((os.path, "realpath"), (os, "lstat"), (os, "stat"), (os, "readlink")):
        real = getattr(mod, name)

        def spy(*a, _real=real, _name=name, **kw):
            counts[_name] += 1
            return _real(*a, **kw)

        monkeypatch.setattr(mod, name, spy)
    # the parent's side of _grep_fallback for a directory: resolve it, walk it, judge each file, hand it to the worker
    real_base = os.path.realpath(tree)
    job = {"pattern": "needle", "budget": 60.0, "base": real_base, "display": str(tree)}
    allowed = tools._walk_filter(tree, real_base, [], [], search_withheld(str(tree), roots))
    feed, sink = tools._Feed(), _Sink()
    tools._feed_worker(sink, job, tools._iter_tree(tree, time.monotonic() + 60), allowed, feed, threading.Event())
    parent = dict(counts)
    monkeypatch.undo()

    files = DIRS * FILES_PER_DIR
    assert feed.sent == files and feed.skipped == 0
    per_file = sum(parent.values()) / files
    print(f"calls per file: {per_file:.2f} { ({k: round(v / files, 2) for k, v in parent.items()}) }")
    assert per_file <= MAX_CALLS_PER_FILE, parent

    # and the worker, run in this process on what the parent sent, finds every match
    stdin = io.BytesIO(sink.getvalue())
    sent_job = json.loads(stdin.readline())
    out: list[list] = []
    grep_worker.run(sent_job, grep_worker._paths(stdin), lambda *msg: out.append(list(msg)))
    hits = sorted(m[1] for m in out if m[0] == "m")
    expected = sorted(
        f"{tree}/pkg{d:02d}/mod{f:03d}.py:1:needle" for d in range(DIRS) for f in range(0, FILES_PER_DIR, 10)
    )
    assert hits == expected
    assert out[-1] == ["end", files, 0, False]
