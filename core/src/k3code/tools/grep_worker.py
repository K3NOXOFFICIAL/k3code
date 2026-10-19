"""The grep fallback's file reader and line matcher, run in a short-lived process of its own (see tools._grep_fallback).

Python's ``re`` holds the GIL for a whole match, and the search deadline can only be looked at between lines: a pattern
the model wrote that backtracks (``(a+)+b`` against one line of forty ``a``) ran for minutes in a thread of the daemon
and froze every session with it. Here it runs in a child the parent kills at the deadline. Standard library only, and
run by its path with ``python -I``: importing the k3code package would cost every search about 0.4 s.

Protocol. stdin: one JSON line (the job: ``pattern``, ``budget`` in seconds, ``display``, and either ``base``, the real
path of the searched directory, or ``fd``, a named file the parent opened and passed down), then the paths to search
relative to ``base``, each followed by a NUL byte, until EOF. stdout: one JSON array per line, ``["m", text]`` for a
matching line (``path:line:text``), ``["n", files_done, long_lines]`` as progress, and last ``["end", files_done,
long_lines, timed_out]``.

The parent decides which files are searched (the tree rules, include and exclude, the credential files a search
withholds); this process opens only the paths it is given, below ``base``, without following a symlink at any level
(see Beneath), so the file it reads is the one the parent decided on by that name.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Iterator
from typing import IO, Any, BinaryIO

#: A file with a NUL byte in its first BINARY_SNIFF_BYTES is binary (as ripgrep decides); files are read in pieces of
#: at most _PIECE_BYTES and the clock is looked at every _CLOCK_BYTES: read_text of a 300 MB binary took 2.6 GB of
#: memory and ran past the time budget.
BINARY_SNIFF_BYTES = 8192
_PIECE_BYTES = 1 << 20
_CLOCK_BYTES = 64 * 1024
#: Longest line rg prints in full; minified bundles and data blobs would otherwise fill the context with one match.
MAX_COLUMNS = 400
#: Longest line (in characters) a regular expression is tried on: a backtracking pattern's cost grows with the line,
#: and a minified bundle is one line of megabytes. A literal pattern (no metacharacter) is searched in any line.
MAX_REGEX_LINE = 20_000
_METACHARACTERS = frozenset(".^$*+?{}[]\\|()")
#: How often (seconds) the progress count is written while files without a match go by.
_PROGRESS_S = 0.05

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY
#: O_NONBLOCK: the open of a FIFO without a writer does not block; O_NOCTTY: a terminal device does not become the
#: process's controlling terminal.
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY


def matcher(pattern: str) -> tuple[Callable[[str], bool], bool]:
    """A test for one line, and whether ``pattern`` is literal: one without a regex metacharacter is a plain substring
    search (no ``re`` at all, so no backtracking and no line-length limit)."""
    if not _METACHARACTERS.intersection(pattern):
        return (lambda line: pattern in line), True
    rx = re.compile(pattern)
    return (lambda line: rx.search(line) is not None), False


class Beneath:
    """Opens regular files below a directory without following a symlink at any level. Every directory on the way is
    opened relative to the one above it, from ``/`` down, with ``O_NOFOLLOW``, and held while files in it are searched;
    the file is opened relative to its directory with ``O_NOFOLLOW`` too. A component that is a link, or was swapped
    for one at any moment before its open, fails (ELOOP, ENOTDIR), so the open file is the one at ``base/rel`` and no
    file outside ``base`` is ever opened: the guarantee a ``/proc/self/fd`` readlink after each open gave before, for
    one ``openat`` per file and one per directory."""

    def __init__(self, base: str) -> None:
        self._base = [p for p in base.split("/") if p]
        self._root = os.open("/", _DIR)
        self._chain: list[tuple[str, int]] = []  # (name, descriptor) of each directory below / that is held

    def open(self, rel: str) -> BinaryIO:
        """``base/rel`` opened for reading; OSError when a component is a link, missing or not a directory, or the
        file is not a regular one (nothing is read from it)."""
        names = rel.split("/")
        if any(n in ("", ".", "..") for n in names):
            raise OSError(errno.EINVAL, "not a plain relative path", rel)
        *dirs, name = [*self._base, *names]
        keep = 0
        while keep < min(len(dirs), len(self._chain)) and self._chain[keep][0] == dirs[keep]:
            keep += 1
        for _, fd in self._chain[keep:]:
            os.close(fd)
        del self._chain[keep:]
        for d in dirs[keep:]:
            self._chain.append((d, os.open(d, _DIR, dir_fd=self._chain[-1][1] if self._chain else self._root)))
        fd = os.open(name, _FILE, dir_fd=self._chain[-1][1] if self._chain else self._root)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "not a regular file", rel)
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        for _, fd in self._chain:
            os.close(fd)
        self._chain.clear()
        os.close(self._root)


def scan(
    f: BinaryIO,
    display: str,
    match: Callable[[str], bool],
    literal: bool,
    deadline: float,
    named: bool,
    stats: dict[str, int],
) -> Iterator[str | None]:
    """``display:line:text`` for each line of the open file ``f`` that ``match`` accepts, streamed, cut to MAX_COLUMNS
    like rg's preview. A binary file found in a walk is skipped; one ``named`` on purpose gives a single ``display:
    binary file matches`` line, as rg says. Past ``deadline`` (``time.monotonic``) it yields None once and stops. A
    line longer than _PIECE_BYTES is searched piece by piece under its own line number (a match across a piece edge, or
    an anchor at one, is approximate); with a regular expression a line longer than MAX_REGEX_LINE is not searched and
    is counted in ``stats["long"]``."""
    binary = b"\0" in f.read(BINARY_SNIFF_BYTES)
    if binary and not named:
        return
    f.seek(0)
    number, line_start, unclocked, too_long = 0, True, 0, False
    for raw in iter(lambda: f.readline(_PIECE_BYTES), b""):
        unclocked += len(raw)
        if unclocked >= _CLOCK_BYTES:
            unclocked = 0
            if time.monotonic() > deadline:
                yield None
                return
        if line_start:  # a piece of a longer line keeps that line's number
            number += 1
            too_long = False
        line_start = raw.endswith(b"\n")
        if too_long:
            continue
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not literal and len(line) > MAX_REGEX_LINE:
            too_long = True
            stats["long"] += 1
            continue
        if not match(line):
            continue
        if binary:
            yield f"{display}: binary file matches"
            return
        if len(line) > MAX_COLUMNS:
            line = line[:MAX_COLUMNS] + " [... omitted end of long line]"
        yield f"{display}:{number}:{line}"


def _paths(stream: IO[bytes]) -> Iterator[str]:
    """The NUL-terminated paths on ``stream`` until EOF."""
    pending = b""
    while chunk := stream.read1(1 << 16):  # type: ignore[attr-defined]
        *done, pending = (pending + chunk).split(b"\0")
        yield from map(os.fsdecode, done)


def _limit_cpu(budget: float) -> None:
    """A CPU-time ceiling a little past the budget: should the parent be gone, the kernel still ends a match that
    never finishes (SIGXCPU, then SIGKILL)."""
    try:
        import resource

        limit = max(1, int(budget) + 3)
        resource.setrlimit(resource.RLIMIT_CPU, (limit, limit + 2))
    except (ImportError, OSError, ValueError):
        pass


def run(job: dict[str, Any], paths: Iterator[str], emit: Callable[..., None]) -> None:
    """Search what ``job`` names (see the module docstring), calling ``emit`` with each message."""
    deadline = time.monotonic() + float(job["budget"])
    match, literal = matcher(job["pattern"])
    stats = {"long": 0}
    files = 0
    timed_out = False
    last = time.monotonic()
    if job.get("fd") is not None:
        with os.fdopen(job["fd"], "rb") as f:
            for hit in scan(f, job["display"], match, literal, deadline, True, stats):
                if hit is None:
                    timed_out = True
                    break
                emit("m", hit)
        files = 0 if timed_out else 1
    else:
        opener = Beneath(job["base"])
        try:
            for rel in paths:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                try:
                    with opener.open(rel) as f:
                        for hit in scan(f, os.path.join(job["display"], rel), match, literal, deadline, False, stats):
                            if hit is None:
                                timed_out = True
                                break
                            emit("m", hit)
                except OSError:  # unreadable, not a regular file, a link on the path, or deleted since it was listed
                    pass
                if timed_out:
                    break
                files += 1
                if (now := time.monotonic()) - last > _PROGRESS_S:
                    last = now
                    emit("n", files, stats["long"])
        finally:
            opener.close()
    emit("end", files, stats["long"], timed_out)


def main() -> None:
    stdin = sys.stdin.buffer
    job = json.loads(stdin.readline())
    _limit_cpu(float(job["budget"]))
    out = sys.stdout

    def emit(*msg: Any) -> None:
        out.write(json.dumps(msg) + "\n")
        out.flush()  # a match is the parent's as soon as it is found: a kill at the deadline loses none

    run(job, _paths(stdin), emit)


if __name__ == "__main__":
    main()
