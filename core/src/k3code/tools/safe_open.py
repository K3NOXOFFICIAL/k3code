"""Open a file the permission engine decided on, so that a path swapped afterwards cannot lead somewhere else.

The engine decides on a path; a tool that then opens that path by name opens whatever the name means a moment
later. A background job in the sandbox can swap a directory of the project for a link to a directory outside every
root in that gap (K3NOX-719). :func:`open_regular` closes it: it walks the path one component at a time from a
directory descriptor, resolves a link itself (reading it, never following it), and holds each directory it entered,
so no later swap above the file changes which file it opens. Before it opens the last component, and again on the
open file, it asks the session's *guard* (see :func:`file_guard`) whether the path it actually reached is allowed:
the same decision the engine makes for that path, so a link that was swapped in leads to a refusal before anything
outside is opened, and a link that was always there (``docs -> ../shared``) still works when its target is allowed.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import os
import stat
from collections import deque
from collections.abc import Callable, Iterator
from typing import BinaryIO

#: A guard takes the real path of a file about to be opened (or just opened) and says whether it may be read.
Guard = Callable[[str], bool]

_GUARD: contextvars.ContextVar[Guard | None] = contextvars.ContextVar("k3code_file_guard", default=None)

MAX_LINKS = 40  # as the kernel: more links than this on one path is ELOOP
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_NOFOLLOW | os.O_CLOEXEC


class NotAllowed(PermissionError):
    """The file the path leads to is not one the permission engine allows (a path component changed)."""


class NotRegular(OSError):
    """The path names something that is not a regular file (a directory, a FIFO, a device)."""


@contextlib.contextmanager
def file_guard(guard: Guard | None) -> Iterator[None]:
    """Make ``guard`` the check every :func:`open_regular` in this context (and the threads it starts) applies."""
    token = _GUARD.set(guard)
    try:
        yield
    finally:
        _GUARD.reset(token)


def _fd_path(fd: int) -> str:
    return os.readlink(f"/proc/self/fd/{fd}")


def _link_target(name: str, dir_fd: int) -> str | None:
    """What the link ``name`` in the directory ``dir_fd`` points at, or None if it is not a link."""
    try:
        if not stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            return None
        return os.readlink(name, dir_fd=dir_fd)
    except OSError:
        return None


def open_regular(path: str | os.PathLike[str], *, guard: Guard | None = None) -> BinaryIO:
    """``path`` (absolute) opened for reading, as a binary file object.

    Raises FileNotFoundError and the other OSErrors an open raises, :class:`NotRegular` when the file is not a regular
    one (nothing is read from it; a FIFO is opened non-blocking and closed again), and :class:`NotAllowed` when the
    ``guard`` (default: the one set by :func:`file_guard`) refuses the path reached. Without ``/proc`` (not Linux) the
    file is opened by name, as before.
    """
    path = os.fspath(path)
    guard = guard or _GUARD.get()
    if not os.path.isdir("/proc/self/fd"):
        return open(path, "rb")  # noqa: SIM115 - the caller closes it
    parts = deque(p for p in path.split("/") if p and p != ".")
    cur = os.open("/", _DIR)
    links = 0
    try:
        while True:
            if not parts:  # the path ended in a link to a directory, or was the root
                raise NotRegular(errno.EISDIR, "not a regular file", path)
            name = parts.popleft()
            last = not parts
            target = None
            if name == "..":
                nxt = os.open("..", _DIR, dir_fd=cur)
                os.close(cur)
                cur = nxt
                if last:
                    raise NotRegular(errno.EISDIR, "not a regular file", path)
                continue
            if not last:
                try:
                    nxt = os.open(name, _DIR, dir_fd=cur)
                except OSError as e:
                    if e.errno not in (errno.ELOOP, errno.ENOTDIR) or (target := _link_target(name, cur)) is None:
                        raise
                else:
                    os.close(cur)
                    cur = nxt
                    continue
            else:
                target = _link_target(name, cur)
                if target is None:
                    candidate = os.path.join(_fd_path(cur), name)
                    if guard is not None and not guard(candidate):
                        raise NotAllowed(errno.EACCES, "not the file the permission check allowed", candidate)
                    try:
                        fd = os.open(name, _FILE, dir_fd=cur)
                    except OSError as e:
                        if e.errno != errno.ELOOP or (target := _link_target(name, cur)) is None:
                            raise
                    else:
                        return _checked(fd, guard, path)
            # a link on the path: its text replaces the component, and what it names is walked the same way
            assert target is not None
            links += 1
            if links > MAX_LINKS:
                raise OSError(errno.ELOOP, "too many levels of symbolic links", path)
            if target.startswith("/"):
                os.close(cur)
                cur = os.open("/", _DIR)
            parts.extendleft(reversed([p for p in target.split("/") if p and p != "."]))
    finally:
        os.close(cur)


def _checked(fd: int, guard: Guard | None, path: str) -> BinaryIO:
    """The open ``fd`` as a file object, if it is a regular file the guard allows (decided on where it really is)."""
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise NotRegular(errno.EISDIR if stat.S_ISDIR(mode) else errno.EINVAL, "not a regular file", path)
        if guard is not None and not guard(_fd_path(fd)):
            raise NotAllowed(errno.EACCES, "not the file the permission check allowed", path)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
