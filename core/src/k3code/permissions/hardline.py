"""Hardline denies: commands that are never allowed, in any mode.

Checked before any rule evaluation (and before modes: even ``yolo`` refuses
them). Extendable from config via ``permissions: {hardline: [...]}``.

The command line is first taken apart with a small quote-aware shell parser (:func:`parse`): simple commands are
split on ``&&  ||  ;  |  &  |&``, newlines and subshell parentheses, and everything nested inside them (``$(...)``,
backticks, ``bash -c "..."``, ``eval``, ``ssh host "..."``) is collected, so a denied command cannot hide behind a
lone ``&``, a quote or a substitution.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

#: (name, regex) pairs matched against the raw command line and every simple command.
HARDLINE_PATTERNS: list[tuple[str, str]] = [
    # rm -rf / and ~ are caught by _rm_check (token based: any flag spelling, quotes, sudo/env wrappers)
    ("mkfs", r"\bmkfs(?:\.\w+)?\b"),  # mkfs / mkfs.ext4 ...
    ("dd-to-device", r"\bdd\b.*\bof=/dev/"),  # dd of=/dev/...
    ("pipe-to-shell", r"\bcurl\b[^|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b"),  # curl ... | sh
    ("wget-pipe-to-shell", r"\bwget\b[^|]*-O\s*-\s*[^|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b"),
    ("env-dump", r"^\s*(?:sudo\s+)?(?:env|printenv)\b"),  # env / printenv
    ("ssh-key-cat", r"\bcat\b[^\n;|&]*\.ssh/"),  # cat ~/.ssh/...
    ("dotenv-cat", r"\bcat\b[^\n;|&]*\.env\b"),  # cat .env / *.env
    ("git-push-force-main", r"\bgit\b[^\n;|&]*\bpush\b(?=[^\n;|&]*--force)(?=[^\n;|&]*\b(?:main|master)\b)"),
]

#: Remote hosts whose services must never be restarted from here. ``ssh <host> "<remote command>"`` can carry
#: ``&&``/``;`` inside the quotes, so these run on each *simple* command (quotes kept together), never on the raw
#: multi-command line, where ``.*`` would mix unrelated commands.
_REMOTE_HOSTS = ("protected-host-a", "protected-host-b")
HARDLINE_SIMPLE_PATTERNS: list[tuple[str, str]] = [
    (f"{host}-{what}-restart", rf"\bssh\b.*\b{host}\b.*\b{tool}\b.*\b(?:restart|stop|kill|down|rm)\b")
    for host in _REMOTE_HOSTS
    for what, tool in (("service", "systemctl"), ("docker", "docker"), ("compose", "docker-compose"))
]

#: Every hardline rule by name, for display (/permissions, the setup wizard): the regex ones, the token-based rm check
#: (see _rm_check) and the per-command ssh checks.
HARDLINE_NAMES: list[str] = [
    "rm-rf-root",
    "rm-rf-home",
    "sensitive-path",  # read/grep/glob or cat/grep/rg/ls of keys, ~/.config/k3code, /proc/*/environ, .env (engine.py)
    *(n for n, _ in HARDLINE_PATTERNS),
    *(n for n, _ in HARDLINE_SIMPLE_PATTERNS),
]

_COMPILED: list[tuple[str, re.Pattern[str]]] = [(name, re.compile(rx)) for name, rx in HARDLINE_PATTERNS]
_COMPILED_SIMPLE: list[tuple[str, re.Pattern[str]]] = [(n, re.compile(r)) for n, r in HARDLINE_SIMPLE_PATTERNS]

#: Wrappers that run what follows (skipped when looking for the real command).
_WRAPPERS = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "nohup",
        "time",
        "nice",
        "ionice",
        "command",
        "builtin",
        "exec",
        "timeout",
        "stdbuf",
        "setsid",
        "chrt",
    }
)
#: Commands that run arbitrary code from their arguments: an "always allow <prefix>" rule must never be offered for
#: them (one click on `python3 *` or `ssh *` would otherwise allow everything).
LAUNCHERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "eval",
        "ssh",
        "xargs",
        "su",
        "watch",
        "find",
        "python",
        "python3",
        "node",
        "perl",
        "ruby",
        "php",
        "lua",
        "awk",
        "gawk",
        "sed",
        "tee",
        "source",
        ".",
        *_WRAPPERS,
    }
)
_DANGEROUS_RM_TARGETS = frozenset(
    {
        "/",
        "/*",
        "~",
        "~/*",
        "$HOME",
        "${HOME}",
        "$HOME/*",
        "${HOME}/*",
        "/home",
        "/etc",
        "/usr",
        "/var",
        "/boot",
        "/bin",
        "/lib",
        "/lib64",
        "/sbin",
        "/root",
        "/opt",
        "/srv",
    }
)


def check(command: str, extra_patterns: list[str] | None = None) -> str | None:
    """Return the hardline rule name if ``command`` is hardline-denied, else None.

    The raw command line is matched first (multi-command patterns such as ``curl | sh`` need it); then every simple
    command and everything nested inside them (``$(...)``, backticks, ``bash -c "..."``, ``ssh host "..."``) gets the
    per-command checks (``ssh`` to a protected host, ``rm`` with a recursive flag on /, ~, $HOME).
    """
    command = _scrub_heredocs(command)  # heredoc bodies are data (a commit message may mention `rm -rf /`)
    hit = _match(command, extra_patterns)
    if hit:
        return hit
    parsed = parse(command)
    for text in (*parsed.subs, *parsed.nested):
        hit = _match(text, extra_patterns) or _simple_check(text)
        if hit:
            return hit
    return None


_HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _is_herestring(line: str, at: int) -> bool:
    """A ``<<`` match that is the tail of ``<<<`` (a here-string: one word, no body)."""
    return at > 0 and line[at - 1] == "<"


def _scrub_heredocs(command: str) -> str:
    """The command line without heredoc bodies (the ``<<EOF`` line stays, the lines up to the terminator go)."""
    if "<<" not in command:
        return command
    out: list[str] = []
    pending: list[str] = []
    for line in command.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending = [m.group(2) for m in _HEREDOC_START.finditer(line) if not _is_herestring(line, m.start())]
    return "\n".join(out)


def _match(text: str, extra_patterns: list[str] | None) -> str | None:
    for name, rx in _COMPILED:
        if rx.search(text):
            return name
    for i, rx in enumerate(extra_patterns or []):
        if re.search(rx, text):
            return f"config-hardline-{i}"
    return None


def _simple_check(sub: str) -> str | None:
    for name, rx in _COMPILED_SIMPLE:
        if rx.search(sub):
            return name
    return _rm_check(sub)


def _tokens(sub: str) -> list[str]:
    try:
        return shlex.split(sub, posix=True)
    except ValueError:
        return sub.split()


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Drop leading sudo/env/nohup/... (with their VAR=x, -flag and numeric arguments) to reach the real command."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        assignment = "=" in t and not t.startswith("=") and t.split("=", 1)[0].isidentifier()
        numeric = i > 0 and tokens[i - 1] in ("timeout", "nice", "ionice") and t[:1].isdigit()
        if t in _WRAPPERS or t.startswith("-") or assignment or numeric:
            i += 1
            continue
        break
    return tokens[i:]


def _rm_check(sub: str) -> str | None:
    """``rm`` with a recursive flag on /, ~, $HOME or a top-level system dir, whatever the flag spelling
    (-rf, -fr, -r -f, -R, --recursive, ...)."""
    tokens = _strip_wrappers(_tokens(sub))
    if not tokens or os.path.basename(tokens[0]) != "rm":
        return None
    flags = [t for t in tokens[1:] if t.startswith("-") and t != "--"]
    recursive = any(t == "--recursive" or (not t.startswith("--") and ("r" in t or "R" in t)) for t in flags)
    if not recursive:
        return None
    for t in tokens[1:]:
        if t.startswith("-"):
            continue
        target = "/" if t and set(t) == {"/"} else (t.rstrip("/") or t)
        if target in _DANGEROUS_RM_TARGETS:
            return "rm-rf-home" if ("~" in t or "HOME" in t or t.startswith("/home")) else "rm-rf-root"
    return None


@dataclass
class Parsed:
    """A shell command line taken apart: the top-level simple commands and everything nested inside them."""

    subs: list[str] = field(default_factory=list)  # split on && || ; | & |& newline ( ), quote-aware
    nested: list[str] = field(default_factory=list)  # inside $(...), `...`, sh -c "...", eval, ssh host "..." (flat)


def _match_paren(text: str, open_idx: int) -> int:
    """Index of the ``)`` that closes the ``(`` at ``open_idx`` (quote/escape aware); len-1 when unbalanced."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            i = (j if j >= 0 else n) + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def _scan(text: str) -> tuple[list[str], list[str]]:
    """Quote-aware split into simple commands, plus the payloads of command substitutions found on the way."""
    subs: list[str] = []
    payloads: list[str] = []
    cur: list[str] = []
    heredocs: list[str] = []
    i, n = 0, len(text)

    def flush() -> None:
        part = "".join(cur).strip()
        if part:
            subs.append(part)
        cur.clear()

    def backtick_end(at: int) -> int:
        end = text.find("`", at + 1)
        return n - 1 if end < 0 else end

    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            cur.append(text[i : i + 2])
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n - 1 if j < 0 else j
            cur.append(text[i : j + 1])
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 2
                elif text.startswith("$(", j):
                    end = _match_paren(text, j + 1)
                    payloads.append(text[j + 2 : end])
                    j = end + 1
                elif text[j] == "`":
                    end = backtick_end(j)
                    payloads.append(text[j + 1 : end])
                    j = end + 1
                else:
                    j += 1
            cur.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("$(", i):
            end = _match_paren(text, i + 1)
            payloads.append(text[i + 2 : end])
            cur.append(text[i : end + 1])
            i = end + 1
        elif c == "`":
            end = backtick_end(i)
            payloads.append(text[i + 1 : end])
            cur.append(text[i : end + 1])
            i = end + 1
        elif text.startswith("<<", i) and not text.startswith("<<<", i):
            m = re.match(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", text[i:])
            if m:
                heredocs.append(m.group(2))
                cur.append(m.group(0))
                i += len(m.group(0))
            else:
                cur.append("<<")
                i += 2
        elif c == "\n":
            flush()
            i += 1
            while heredocs:  # a heredoc body is data, not commands: skip to its terminator line
                delim = heredocs.pop(0)
                while i < n:
                    eol = text.find("\n", i)
                    eol = n if eol < 0 else eol
                    line = text[i:eol].strip()
                    i = eol + 1
                    if line == delim:
                        break
        elif text[i : i + 2] in ("&&", "||", "|&"):
            flush()
            i += 2
        elif c in ";|":
            flush()
            i += 1
        elif c == "&":
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if prev in "<>" or nxt == ">":  # 2>&1, >&2, &>file: part of a redirection
                cur.append(c)
            else:
                flush()  # a lone & backgrounds the command; what follows is another command
            i += 1
        elif c in "()":
            flush()
            i += 1
        else:
            cur.append(c)
            i += 1
    flush()
    return subs, payloads


def _launcher_payloads(sub: str) -> list[str]:
    """Quoted arguments of ``bash -c``, ``eval``, ``ssh host "..."``, ``sudo sh -c ...``: strings run as commands."""
    real = _strip_wrappers(_tokens(sub))
    if not real or os.path.basename(real[0]) not in LAUNCHERS:
        return []
    return [t for t in real[1:] if not t.startswith("-") and re.search(r"\s|[;&|$`<>()]", t)]


def parse(command: str, _depth: int = 0) -> Parsed:
    """Split ``command`` into simple commands and collect every command nested inside it (recursively)."""
    subs, payloads = _scan(command)
    parsed = Parsed(subs=subs)
    if _depth >= 6:  # pathological nesting is cut off; the caller still checks the raw text
        return parsed
    for sub in subs:
        payloads.extend(_launcher_payloads(sub))
    for payload in payloads:
        inner = parse(payload, _depth + 1)
        parsed.nested.extend([*inner.subs, *inner.nested])
    return parsed


def split_commands(command: str) -> list[str]:
    """Split a shell command into simple commands (quote-aware: ``&&``, ``||``, ``;``, ``|``, ``&``, ``|&``,
    newlines and subshell parentheses split; quoted text, substitutions and heredoc bodies stay together)."""
    return parse(command).subs


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


def is_launcher(sub_command: str) -> bool:
    """True when the command runs code given in its arguments (shells, ssh, interpreters, xargs, find, sudo ...)."""
    tokens = _tokens(sub_command)
    if tokens and os.path.basename(tokens[0]) in LAUNCHERS:
        return True
    real = _strip_wrappers(tokens)
    return bool(real) and os.path.basename(real[0]) in LAUNCHERS


#: Shell tokenising for the engine's argument checks (quote-aware, wrappers such as sudo/env stripped).
tokens = _tokens
strip_wrappers = _strip_wrappers
