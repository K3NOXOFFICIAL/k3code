"""The grep fallback's walk judges each file by its name below the resolved directories it entered (no realpath per
file, see tools._walk_filter and search_withheld's ``real``) and leaves the refusal of links to its worker. That
shortcut must withhold exactly what the realpath check did: a credential file below an allowed directory outside the
roots, a dotenv file, and a link to a file outside, also one swapped in after the walk judged the name."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

import k3code.tools as tools
import test_permission_outside_walk as outside_walk
from k3code.permissions import decide
from k3code.permissions.engine import search_withheld
from k3code.tools import grep_worker, tool_grep
from test_permission_outside_walk import MARKER, SECRET, SECRET_FILES, _assert_clean

world = outside_walk.world  # the fake $HOME with credential files below ~/projects/other, and the session's cwd

#: Planted below ~/projects/other next to the fixture's SECRET_FILES.
MORE_SECRETS = ("id_rsa", ".env", "deep/er/.env")


class _Sink(io.BytesIO):
    def close(self) -> None:  # the feeder closes the worker's stdin; keep it readable
        pass


@pytest.fixture
def other(world, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """~/projects/other (outside the roots) with more credential files, links to a file and a directory outside it,
    and an ordinary file and directory a test swaps for links later. No rg, no git: the fallback's own walk."""
    home, _ = world
    other = home / "projects" / "other"
    for rel in MORE_SECRETS:
        (other / rel).parent.mkdir(parents=True, exist_ok=True)
        (other / rel).write_text(f"KEY={SECRET}\n")
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "secret.txt").write_text(f"{MARKER} {SECRET} outside\n")
    (outside / "dir" / "a.txt").write_text(f"{MARKER} {SECRET} outside dir\n")
    (other / "link.txt").symlink_to(outside / "secret.txt")
    (other / "linkdir").symlink_to(outside / "dir")
    (other / "swap.txt").write_text(f"{MARKER} swap file\n")
    (other / "src").mkdir()
    (other / "src" / "a.txt").write_text(f"{MARKER} src file\n")
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    return other


def _clean(text: str) -> None:
    _assert_clean(text)
    assert "outside" not in text and "id_rsa" not in text and ".env" not in text, text


@pytest.mark.parametrize("mode", ["accept-edits", "default"])
async def test_the_fallback_walk_returns_no_credential_file_and_follows_no_link(other, world, mode) -> None:
    _, cwd = world
    d = decide(mode=mode, tool="grep", args={"path": str(other), "pattern": "*"}, cwd=str(cwd))
    assert d.action in ("allow", "ask"), d  # default asks a person for a path outside the roots; then the same walk
    for include in (None, "*"):
        args = {"pattern": f"{SECRET}|{MARKER}", "path": str(other), **({"include": include} if include else {})}
        res = await tool_grep(args, cwd=cwd)
        assert "error" not in res, res
        _clean(res["matches"])
        assert f"notes.txt:1:an ordinary {MARKER} line" in res["matches"], res
        assert f"src/a.txt:1:{MARKER} src file" in res["matches"], res


def test_a_file_or_directory_swapped_for_a_link_after_the_walk_judged_it_is_not_read(other, world) -> None:
    _, cwd = world
    real_base = os.path.realpath(other)
    allowed = tools._walk_filter(other, real_base, [], [], search_withheld(str(other), [str(cwd)]))
    job = {"pattern": f"{SECRET}|{MARKER}", "budget": 30.0, "base": real_base, "display": str(other)}
    feed, sink = tools._Feed(), _Sink()
    tools._feed_worker(sink, job, tools._iter_tree(other, time.monotonic() + 30), allowed, feed, threading.Event())
    stdin = io.BytesIO(sink.getvalue())
    sent_job = json.loads(stdin.readline())
    sent = stdin.read().split(b"\0")
    names = {os.fsdecode(n) for n in sent if n}
    assert {"notes.txt", "swap.txt", "src/a.txt"} <= names
    assert not names & {*SECRET_FILES, *MORE_SECRETS}, names
    assert feed.skipped == len(SECRET_FILES) - 1 + len(MORE_SECRETS)  # each one counted; .git/ is never walked
    # what the walk judged ordinary becomes a link to a file outside before the worker opens it
    tmp = other.parent.parent.parent.parent
    (other / "swap.txt").unlink()
    (other / "swap.txt").symlink_to(tmp / "outside" / "secret.txt")
    (other / "src").rename(other / "src.old")
    (other / "src").symlink_to(tmp / "outside" / "dir")
    out: list[list] = []
    grep_worker.run(sent_job, grep_worker._paths(io.BytesIO(b"\0".join(sent))), lambda *m: out.append(list(m)))
    hits = "\n".join(m[1] for m in out if m[0] == "m")
    _clean(hits)
    assert f"notes.txt:1:an ordinary {MARKER} line" in hits
    assert "swap.txt" not in hits and "src/a.txt" not in hits, hits
