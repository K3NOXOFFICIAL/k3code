"""Hardline denies: commands that are never allowed, in any mode.

Checked before any rule evaluation (and before modes: even ``yolo`` refuses
them). Extendable from config via ``permissions: {hardline: [...]}``.
"""

from __future__ import annotations

import re
import shlex

#: (name, regex) pairs matched against each bash sub-command string.
HARDLINE_PATTERNS: list[tuple[str, str]] = [
    ("rm-rf-root", r"\brm\b.*\s-rf?\s+/(?:\s|$)"),  # rm -rf / variants
    ("rm-rf-home", r"\brm\b.*\s-rf?\s+~(?:/|(?:\s|$))"),  # rm -rf ~ variants
    ("mkfs", r"\bmkfs(?:\.\w+)?\b"),  # mkfs / mkfs.ext4 ...
    ("dd-to-device", r"\bdd\b.*\bof=/dev/"),  # dd of=/dev/...
    ("pipe-to-shell", r"\bcurl\b[^|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b"),  # curl ... | sh
    ("wget-pipe-to-shell", r"\bwget\b[^|]*-O\s*-\s*[^|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b"),
    ("env-dump", r"^\s*(?:sudo\s+)?(?:env|printenv)\b"),  # env / printenv
    ("ssh-key-cat", r"\bcat\b[^\n;|&]*\.ssh/"),  # cat ~/.ssh/...
    ("dotenv-cat", r"\bcat\b[^\n;|&]*\.env\b"),  # cat .env / *.env
    ("git-push-force-main", r"\bgit\b[^\n;|&]*\bpush\b(?=[^\n;|&]*--force)(?=[^\n;|&]*\b(?:main|master)\b)"),
    ("protected-host-a-service-restart", r"\bssh\b[^\n;|&]*\bprotected-host-a\b[^\n;|&]*\bsystemctl\b[^\n;|&]*\b(?:restart|stop)\b"),
    ("protected-host-a-docker-restart", r"\bssh\b[^\n;|&]*\bprotected-host-a\b[^\n;|&]*\bdocker\b[^\n;|&]*\b(?:restart|stop)\b"),
    ("protected-host-b-service-restart", r"\bssh\b[^\n;|&]*\bprotected-host-b\b[^\n;|&]*\bsystemctl\b[^\n;|&]*\b(?:restart|stop)\b"),
    ("protected-host-b-docker-restart", r"\bssh\b[^\n;|&]*\bprotected-host-b\b[^\n;|&]*\bdocker\b[^\n;|&]*\b(?:restart|stop)\b"),
]

_COMPILED: list[tuple[str, re.Pattern[str]]] = [(name, re.compile(rx)) for name, rx in HARDLINE_PATTERNS]


def check(command: str, extra_patterns: list[str] | None = None) -> str | None:
    """Return the hardline rule name if ``command`` is hardline-denied, else None."""
    for name, rx in _COMPILED:
        if rx.search(command):
            return name
    for i, rx in enumerate(extra_patterns or []):
        if re.search(rx, command):
            return f"config-hardline-{i}"
    return None


def split_commands(command: str) -> list[str]:
    """Split a shell command into sub-commands on &&, ||, ;, | and newlines."""
    parts = re.split(r"&&|\|\||[;|\n]", command)
    return [p.strip() for p in parts if p.strip()]


def command_prefix(sub_command: str) -> str:
    """Narrowest arity prefix string for an ``always`` rule suggestion.

    ``git commit -m x`` -> ``"git commit"``; unknown commands -> first token.
    Flags never count: they are stripped before arity lookup.
    """
    try:
        tokens = shlex.split(sub_command, posix=True)
    except ValueError:
        tokens = sub_command.split()
    tokens = [t for t in tokens if not t.startswith("-") or t in ("-", "--")]
    if not tokens:
        return ""
    from .arity import prefix

    return " ".join(prefix(tokens))
