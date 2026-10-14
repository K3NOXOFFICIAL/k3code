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
    # (?i): drvfs (/mnt/c) and macOS open ~/.ssh for ~/.SSH and .env for .ENV
    ("ssh-key-cat", r"(?i)\bcat\b[^\n;|&]*\.ssh/"),  # cat ~/.ssh/...
    # cat .env / *.env, but not the committed templates (.env.example ...)
    ("dotenv-cat", r"(?i)\bcat\b[^\n;|&]*\.env\b(?!\.(?:example|sample|template|dist)\b)"),
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
    # read/grep/glob, or a bash argument, < source or glob that reads or sends keys, ~/.config/k3code,
    # /proc/*/environ, .env (engine.py: printing, testing, chmod, ssh -i and a copy destination are not reads;
    # rm, source, git add and --env-file ask a person)
    "sensitive-path",
    "fetch-and-run",  # a shell/interpreter running a $(...), `...` or <(...) that downloads (see _fetch_run_check)
    "git-push-mirror",
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
#: Commands whose arguments name what runs (a package, a module, a script, a target, a container): a learned or
#: proposed ``<prefix> *`` rule (``uv *``, ``npx *``, ``docker run *``) would allow anything at all, so they are only
#: ever proposed as the exact command. Separate from LAUNCHERS, which also drives payload parsing.
WILDCARD_UNSAFE = frozenset(
    {"uv", "uvx", "npx", "bunx", "deno", "node", "perl", "ruby", "php", "sh", "bash", "zsh", "env", "xargs", "make"}
)
_WILDCARD_UNSAFE_PREFIXES = ("pip", "python")
_WILDCARD_UNSAFE_PAIRS = frozenset(
    {
        ("pnpm", "dlx"),
        ("pnpm", "exec"),
        ("bun", "x"),
        ("npm", "exec"),
        ("docker", "run"),
        ("docker", "exec"),
        ("podman", "run"),
        ("podman", "exec"),
        ("kubectl", "exec"),
    }
)


def wildcard_unsafe(command: str) -> bool:
    """``command`` (or a rule pattern) starts with a command that must never get a ``<prefix> *`` rule (see
    WILDCARD_UNSAFE), including ``git -c key=value ...`` (a config value can name a program to run)."""
    toks = _tokens(command)
    while toks and _is_assignment(toks[0]):
        toks = toks[1:]
    if not toks:
        return False
    root = os.path.basename(toks[0])
    if root in WILDCARD_UNSAFE or root.startswith(_WILDCARD_UNSAFE_PREFIXES):
        return True
    if root == "git":
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            if toks[i].startswith(("-c", "--config-env")):
                return True
            i += 2 if toks[i] in _GIT_VALUE_OPTS else 1
        return False
    operands = [t for t in toks[1:] if not t.startswith("-")]
    return bool(operands) and (root, operands[0]) in _WILDCARD_UNSAFE_PAIRS


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


def check(
    command: str, extra_patterns: list[str] | None = None, *, cwd: str | None = None, home: str | None = None
) -> str | None:
    """Return the hardline rule name if ``command`` is hardline-denied, else None.

    The regexes run on the cleaned command line (comments, line continuations and heredoc bodies removed) and on every
    simple command, its normalised argv (:func:`normalize_argv`) and everything nested inside it (``$(...)``,
    backticks, ``<(...)``, ``bash -c "..."``, ``eval``, ``ssh host "..."``, the substitutions of an unquoted heredoc
    body and the script a heredoc feeds to a shell). The structural checks (rm targets, git
    push, environment dumps, downloads fed to a shell) run on the normalised argv and the pipelines. ``cwd`` and
    ``home`` expand ``$PWD``, ``~`` and ``$HOME`` in rm targets (home defaults to the real one).
    """
    home = home if home is not None else os.path.expanduser("~")
    parsed = parse(command)
    return _match(parsed.clean, extra_patterns) or _check_parsed(parsed, extra_patterns, cwd, home)


def _check_parsed(parsed: Parsed, extra: list[str] | None, cwd: str | None, home: str) -> str | None:
    for k, sub in enumerate(parsed.subs):
        argv = normalize_argv(_tokens(sub))
        hit = (
            _match(sub, extra)
            or _match(" ".join(argv), extra)
            or _simple_check(sub)
            or _rm_check(argv, cwd, home)
            or _git_push_check(argv)
            or _env_dump_check(argv)
            or _fetch_run_check(argv, parsed.sub_payloads[k])
        )
        if hit:
            return hit
    hit = _pipeline_check(parsed.pipelines)
    if hit:
        return hit
    for child in parsed.children:
        hit = _match(child.clean, extra) or _check_parsed(child, extra, cwd, home)
        if hit:
            return hit
    return None


#: A pending heredoc: (delimiter, ``<<-`` strips leading tabs, quoted).
HeredocWord = tuple[str, bool, bool]


def _heredoc_word(text: str, i: int) -> tuple[int, HeredocWord | None]:
    """The ``<<``/``<<-`` at ``i`` and its delimiter word, read the way bash reads it: the whole word up to a blank or
    an operator, quotes removed; a quote or a backslash anywhere in it makes the body data (``E'OF'`` is ``EOF``,
    quoted). Returns the index after the word, and None when the word is missing or bash versions read it differently
    (``$'EOF'``, ``\\<newline>``, an open quote, ``$(``): the caller then cannot tell where the body ends."""
    tabs = text.startswith("<<-", i)
    i += 3 if tabs else 2
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    start, word, quoted = i, [], False
    while i < n and text[i] not in " \t\n;&|()<>":
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] != "\n":
            word.append(text[i + 1])
            quoted, i = True, i + 2
        elif c in "'\"" and (j := text.find(c, i + 1)) > 0 and not (c == '"' and re.search(r"[\\$`]", text[i:j])):
            word.append(text[i + 1 : j])
            quoted, i = True, j + 1
        elif c in "\\'\"`" or text.startswith(("$'", '$"', "$(", "${", "$["), i) or (c == "#" and i == start):
            return i, None
        else:
            word.append(c)
            i += 1
    return i, (("".join(word), tabs, quoted) if i > start else None)


def _skip_heredoc_bodies(text: str, i: int, delims: list[HeredocWord], bodies: list[str] | None = None) -> int:
    """From the start of a line at ``i``, skip the bodies of the pending heredocs ``delims`` (consumed). A body ends at
    the first line that is exactly its delimiter (``<<-`` strips leading tabs only); in an unquoted body a line that
    ends in an odd number of backslashes runs on into the next before that comparison, as in bash. ``bodies``
    collects the text of each body, without its delimiter line."""
    n = len(text)
    while delims:
        delim, tabs, quoted = delims.pop(0)
        start = end = i
        while i < n:
            end, line = i, ""
            while True:
                eol = text.find("\n", i)
                eol = n if eol < 0 else eol
                part, i = text[i:eol], eol + 1
                if quoted or (len(part) - len(part.rstrip("\\"))) % 2 == 0 or i >= n:
                    line += part
                    break
                line += part[:-1]
            if (line.lstrip("\t") if tabs else line) == delim:
                break
        else:
            end = n  # no delimiter line: the body runs to the end of the text
        if bodies is not None:
            bodies.append(text[start:end])
    return i


def _subst_payload(text: str, at: int, end: int) -> str:
    """The payload of the ``$(``/``<(``/``>(`` at ``at`` that closes at ``end``. ``$((...))`` is arithmetic: its payload
    is the ``((...))`` command it equals, where a ``<<`` is a shift."""
    return text[at + 1 : end + 1] if text.startswith("$((", at) else text[at + 2 : end]


def _body_payloads(body: str) -> tuple[list[str], bool]:
    """The ``$(...)`` and backtick payloads of an unquoted heredoc body (the shell runs them), and whether one is
    unterminated. Quotes mean nothing in a body, a backslash escapes the next character (``\\$(x)`` is text) and
    ``\\<newline>`` is removed, as on the command line (``$\\<newline>(x)`` runs)."""
    body = re.sub(r"\\([\s\S])", lambda m: "" if m.group(1) == "\n" else m.group(0), body)
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            i += 2
        elif body.startswith("$(", i):
            end = _match_paren(body, i + 1)
            if end < 0:
                return [*out, body[i + 2 :]], True
            out.append(_subst_payload(body, i, end))
            i = end + 1
        elif c == "`":
            j = i + 1
            while j < n and body[j] != "`":
                j += 2 if body[j] == "\\" else 1
            if j >= n:
                return [*out, body[i + 1 :]], True
            out.append(body[i + 1 : j])
            i = j + 1
        else:
            i += 1
    return out, False


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
    return None


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


def _is_assignment(t: str) -> bool:
    return "=" in t and not t.startswith("=") and t.split("=", 1)[0].isidentifier()


#: Wrapper option arity for :func:`normalize_argv`: (short options taking a value, long options taking a value,
#: positional arguments before the wrapped command).
_WRAPPER_SPECS: dict[str, tuple[str, frozenset[str], int]] = {
    "timeout": ("sk", frozenset({"--signal", "--kill-after"}), 1),
    "nice": ("n", frozenset({"--adjustment"}), 0),
    "sudo": (
        "ugpCDrtUT",
        frozenset({"--user", "--group", "--prompt", "--chdir", "--close-from", "--role", "--type", "--other-user"}),
        0,
    ),
    "doas": ("uC", frozenset(), 0),
    "env": ("uSC", frozenset({"--unset", "--split-string", "--chdir"}), 0),
    "xargs": (
        "nIPdLaEs",
        frozenset({"--max-args", "--max-procs", "--delimiter", "--max-lines", "--arg-file", "--eof", "--max-chars"}),
        0,
    ),
    "stdbuf": ("ioe", frozenset({"--input", "--output", "--error"}), 0),
    "ionice": ("cnpPu", frozenset({"--class", "--classdata", "--pid", "--pgid", "--uid"}), 0),
    "nohup": ("", frozenset(), 0),
    "exec": ("a", frozenset(), 0),
    "command": ("", frozenset(), 0),
    "builtin": ("", frozenset(), 0),
    "time": ("fo", frozenset({"--format", "--output"}), 0),
    "chrt": ("TPD", frozenset({"--sched-runtime", "--sched-period", "--sched-deadline"}), 1),
    "taskset": ("", frozenset(), 1),
    "setsid": ("", frozenset(), 0),
}
#: Wrapper options whose value names a file the wrapper reads and hands on as text: ``xargs -a FILE echo`` prints FILE.
_WRAPPER_READ_OPTS: dict[str, tuple[str, frozenset[str]]] = {"xargs": ("a", frozenset({"--arg-file"}))}
#: git options before the subcommand that take a value (``git -C dir push`` is ``git push``).
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"})
#: ssh client short options taking a value.
SSH_VALUE_OPTS = {"ssh": "BbcDEeFIiJLlmOoPpQRSWw", "scp": "cDFiJloPSX", "sftp": "BbcDFiJloPRSsX"}
#: ``-o Keyword=value`` / ``-o 'Keyword value'`` whose value is a command line (ProxyCommand, LocalCommand,
#: RemoteCommand, KnownHostsCommand).
_SSH_COMMAND_OPTION = re.compile(r"\s*[A-Za-z]*command(?:\s*=|\s)\s*(.*)", re.IGNORECASE | re.DOTALL)


def ssh_command_value(value: str) -> str | None:
    """The command line in an ssh ``-o`` value whose keyword ends in ``Command``, else None."""
    m = _SSH_COMMAND_OPTION.match(value)
    return m.group(1) if m else None


#: rsync short options that take a value (``-e`` is the remote shell; the others only need skipping).
_RSYNC_VALUE_OPTS = "BefTM@"


def _rsync_shell_commands(args: list[str]) -> list[str]:
    """The remote-shell command lines of ``rsync -e CMD`` / ``-eCMD`` / ``-avze CMD`` / ``--rsh=CMD`` /
    ``--rsh CMD``: rsync runs them."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            break
        if a == "--rsh" and i < len(args):
            out.append(args[i])
            i += 1
        elif a.startswith("--rsh="):
            out.append(a.split("=", 1)[1])
        elif a.startswith("-") and not a.startswith("--"):
            for k, ch in enumerate(a[1:], 1):
                if ch in _RSYNC_VALUE_OPTS:
                    value = a[k + 1 :]
                    if not value and i < len(args):
                        value, i = args[i], i + 1
                    if ch == "e":
                        out.append(value)
                    break
    return out


def ssh_option_commands(name: str, args: list[str]) -> list[str]:
    """The command lines ``ssh``/``scp``/``sftp`` run from their ``-o XxxCommand`` options, glued
    (``-oProxyCommand=...``, ``-vo...``) or spaced, and the remote shell of ``rsync -e``/``--rsh`` (a command line
    too: ``rsync -e "ssh -oProxyCommand='head .env'"`` runs the inner command). ssh parses options after the host name
    too, so every argument is scanned."""
    if name == "rsync":
        return _rsync_shell_commands(args)
    short_val = SSH_VALUE_OPTS.get(name)
    out: list[str] = []
    i = 0
    while short_val and i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            break
        if not a.startswith("-") or a.startswith("--"):
            continue
        for k, ch in enumerate(a[1:], 1):
            if ch in short_val:
                value = a[k + 1 :]
                if not value and i < len(args):
                    value, i = args[i], i + 1
                if ch == "o" and (command := ssh_command_value(value)) is not None:
                    out.append(command)
                break
    return out


def _skip_wrapper_options(
    name: str, args: list[str], spec: tuple[str, frozenset[str], int], reads: list[str] | None = None
) -> list[str]:
    short_val, long_val, positionals = spec
    read_short, read_long = _WRAPPER_READ_OPTS.get(name, ("", frozenset()))
    prefix: list[str] = []  # env -S "cmd args" splits its value into the command
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if name == "env" and (a == "-" or _is_assignment(a)):
            i += 1
            continue
        if a.startswith("--"):
            key, eq, value = a.partition("=")
            if key in long_val and not eq:
                i += 1
                value = args[i] if i < len(args) else ""
            if name == "env" and key == "--split-string":
                prefix += _tokens(value)
            if key in read_long and value and reads is not None:
                reads.append(value)
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch in short_val:
                    value = a[k + 1 :]
                    if not value:
                        i += 1
                        value = args[i] if i < len(args) else ""
                    if name == "env" and ch == "S":
                        prefix += _tokens(value)
                    if ch in read_short and value and reads is not None:
                        reads.append(value)
                    break
            i += 1
            continue
        break
    return prefix + args[i + positionals :]


def normalize_argv(tokens: list[str], reads: list[str] | None = None) -> list[str]:
    """The command that really runs: leading ``VAR=x`` assignments and wrappers (sudo, env, nice, timeout, xargs,
    ...) with their options dropped, ``eval`` arguments re-split, git's global options (``-C dir``, ``-c k=v``, ...)
    removed and argv0 reduced to its basename. A wrapper with nothing left to run is itself the command (``env``).
    ``reads`` collects the files a dropped wrapper option reads (``xargs -a FILE``, see _WRAPPER_READ_OPTS)."""
    argv = list(tokens)
    for _ in range(len(tokens) + 1):  # every round drops at least one token
        while argv and _is_assignment(argv[0]):
            argv = argv[1:]
        if not argv:
            return []
        name = os.path.basename(argv[0]) or argv[0]
        argv = [name, *argv[1:]]
        if name == "eval" and len(argv) > 1:
            argv = _tokens(" ".join(argv[1:]))
            continue
        if name == "git":
            i = 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] in _GIT_VALUE_OPTS else 1
            return [name, *argv[i:]]
        spec = _WRAPPER_SPECS.get(name)
        if spec is None:
            return argv
        rest = _skip_wrapper_options(name, argv[1:], spec, reads)
        if not rest:
            return argv
        argv = rest
    return argv


#: Commands that download content, and commands that run what they are fed as code.
_FETCHERS = frozenset({"curl", "wget", "aria2c", "http", "https", "xh", "xhs", "fetch"})
_INTERPRETERS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "node", "perl", "ruby", "php", "deno", "bun", "source", ".", "eval"}
)


def _is_interpreter(name: str) -> bool:
    return name in _INTERPRETERS or name.startswith("python")


#: Paths that name the process's own stdin as a script operand.
_STDIN_PATHS = frozenset({"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
#: Per interpreter family: (short options whose argument is the code: stdin is then data, short options taking a value,
#: long options whose argument is the code, long options taking a value).
_STDIN_SPECS: dict[str, tuple[str, str, frozenset[str], frozenset[str]]] = {
    "shell": ("c", "oO", frozenset({"--command"}), frozenset({"--rcfile", "--init-file", "--init-command"})),
    "python": ("cm", "WX", frozenset(), frozenset({"--check-hash-based-pycs"})),
    "node": ("ep", "r", frozenset({"--eval", "--print"}), frozenset({"--require", "--import", "--loader"})),
    "perl": ("eE", "IMm", frozenset(), frozenset()),
    "ruby": ("eE", "rICEx", frozenset(), frozenset()),
    "php": ("rRBEF", "cdz", frozenset(), frozenset()),
}


def _stdin_family(name: str) -> str | None:
    if name in _SHELLS:
        return "shell"
    if name.startswith("python"):
        return "python"
    return name if name in _STDIN_SPECS else None


def _runs_stdin_as_code(argv: list[str]) -> bool:
    """The interpreter in ``argv`` executes what arrives on stdin as code: a shell with no script operand or with
    ``-s``, an interpreter with no script operand or with ``-``. ``python3 -m json.tool``, ``node -e ...`` or a script
    file read stdin as data. Unknown spellings (deno, bun, source, eval) count as code."""
    name = argv[0] if argv else ""
    family = _stdin_family(name)
    if family is None:
        return _is_interpreter(name)
    code_short, value_short, code_long, value_long = _STDIN_SPECS[family]
    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in _STDIN_PATHS:
            return True
        if a == "--":
            return i + 1 >= len(args) or args[i + 1] in _STDIN_PATHS
        if a.startswith("--"):
            key, eq, _ = a.partition("=")
            if key in code_long:
                return False
            i += 2 if key in value_long and not eq else 1
            continue
        if a.startswith("-") or (family == "shell" and a.startswith("+") and len(a) > 1):
            skip = 1
            for k, ch in enumerate(a[1:], 1):
                if family == "shell" and ch == "s" and a[0] == "-":
                    return True
                if ch in code_short:
                    return False
                if ch in value_short:
                    skip += 0 if a[k + 1 :] else 1
                    break
            i += skip
            continue
        return False  # a script file operand: stdin is its input
    return True  # no script operand: the interpreter reads its program from stdin


def _pipeline_check(pipelines: list[list[str]]) -> str | None:
    """A download fed into a later stage of the same pipeline that runs stdin as code (``curl x | sudo -n sh``,
    ``curl x | python3 -``); ``curl x | python3 -m json.tool`` only reads it."""
    for stages in pipelines:
        fetched = False
        for stage in stages:
            argv = normalize_argv(_tokens(stage))
            name = argv[0] if argv else ""
            if fetched and _is_interpreter(name) and _runs_stdin_as_code(argv):
                return "pipe-to-shell"
            fetched = fetched or name in _FETCHERS
    return None


def _fetch_run_check(argv: list[str], payloads: list[str]) -> str | None:
    """A shell/interpreter/eval (or a substitution used as the command itself) running a ``$(...)``, backtick or
    ``<(...)`` that downloads: ``bash <(curl x)``, ``sh -c "$(curl x)"``."""
    if not argv or not payloads:
        return None
    if not (_is_interpreter(argv[0]) or argv[0].startswith(("$(", "`", "<("))):
        return None
    for payload in payloads:
        inner = parse(payload)
        for s in (*inner.subs, *inner.nested):
            if normalize_argv(_tokens(s))[:1] and normalize_argv(_tokens(s))[0] in _FETCHERS:
                return "fetch-and-run"
    return None


_MAIN_REFS = frozenset({"main", "master", "refs/heads/main", "refs/heads/master"})


def _git_push_check(argv: list[str]) -> str | None:
    """``git push`` with a force flag or a ``+refspec`` onto main/master, or ``--mirror``. A force push without a
    refspec (the upstream, unknown here) is left to the regex layer."""
    if argv[:2] != ["git", "push"]:
        return None
    args = argv[2:]
    if "--mirror" in args:
        return "git-push-mirror"
    force = False
    positional: list[str] = []
    for a in args:
        long_force = a.split("=", 1)[0] in ("--force", "--force-with-lease", "--force-if-includes")
        if long_force or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]):
            force = True
        elif not a.startswith("-"):
            positional.append(a)
    for refspec in positional[1:]:
        if refspec.lstrip("+").split(":")[-1] in _MAIN_REFS and (force or refspec.startswith("+")):
            return "git-push-force-main"
    return None


def _env_dump_check(argv: list[str]) -> str | None:
    """``env`` / ``printenv`` / ``export -p`` / ``set`` printing the environment (secrets) to the transcript."""
    if not argv:
        return None
    name, args = argv[0], argv[1:]
    if name in ("env", "printenv") or (name == "export" and set(args) <= {"-p"}) or (name == "set" and not args):
        return "env-dump"
    return None


_HOME_PWD = re.compile(r"\$\{(HOME|PWD)\}|\$(HOME|PWD)(?![A-Za-z0-9_])")


def expand_vars(word: str, cwd: str | None, home: str) -> str:
    """``word`` with a leading ``~`` and every ``$HOME``/``${HOME}`` (and ``$PWD``/``${PWD}`` when ``cwd`` is known)
    expanded; other variables stay as written."""
    if word == "~" or word.startswith("~/"):
        word = home + word[1:]

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return home if name == "HOME" else (cwd if cwd else m.group(0))

    return _HOME_PWD.sub(sub, word)


def _expand_target(t: str, cwd: str | None, home: str) -> str:
    if t == "~" or t.startswith("~/"):
        t = home + t[1:]
    t = t.replace("${HOME}", home).replace("$HOME", home)
    if cwd:
        t = t.replace("${PWD}", cwd).replace("$PWD", cwd)
    glob = t.endswith("/*")
    base = t[:-2] if glob else t
    if base:
        base = os.path.normpath(base)
        if base.startswith("//"):
            base = "/" + base.lstrip("/")
    return base + "/*" if glob else base


def _rm_check(argv: list[str], cwd: str | None, home: str) -> str | None:
    """``rm`` with a recursive flag on /, ~, $HOME or a top-level system dir, whatever the flag spelling
    (-rf, -fr, -r -f, -R, --recursive, ...) or path spelling (``/.``, ``~/.``, ``/usr/..``, ``$PWD`` at /)."""
    if not argv or argv[0] != "rm":
        return None
    flags: list[str] = []
    targets: list[str] = []
    done = False
    for t in argv[1:]:
        if not done and t == "--":
            done = True
        elif not done and t.startswith("-") and t != "-":
            flags.append(t)
        else:
            targets.append(t)
    recursive = any(t == "--recursive" or (not t.startswith("--") and ("r" in t or "R" in t)) for t in flags)
    if not recursive:
        return None
    dangerous = _DANGEROUS_RM_TARGETS | {home, home + "/*"}
    for t in targets:
        target = _expand_target(t, cwd, home) if t else t
        if target in dangerous:
            homeish = "~" in t or "HOME" in t or t.startswith("/home") or target in (home, home + "/*")
            return "rm-rf-home" if homeish else "rm-rf-root"
    return None


@dataclass
class Heredoc:
    """A heredoc fed to a command. Its body is data when the delimiter is quoted; an unquoted body is expanded by the
    shell, so the ``$(...)`` and backtick ``payloads`` in it run."""

    body: str
    quoted: bool
    payloads: list[str] = field(default_factory=list)


@dataclass
class Parsed:
    """A shell command line taken apart: the top-level simple commands and everything nested inside them."""

    subs: list[str] = field(default_factory=list)  # split on && || ; | & |& newline ( ), quote-aware
    nested: list[str] = field(default_factory=list)  # inside $(...), `...`, <(...), sh -c "...", eval, ssh (flat)
    #: an open quote, $(, <( or backtick at the end: what the shell does with the rest is not what we parsed
    unterminated: bool = False
    #: a comment that contains a quote character (an old trick to hide the next line inside a "quote")
    comment_quote: bool = False
    pipelines: list[list[str]] = field(default_factory=list)  # the subs grouped by | and |&
    clean: str = ""  # the line without comments, line continuations and heredoc bodies (what the regexes see)
    #: $(...)/`...`/<(...) payloads per sub, those of its unquoted heredoc bodies included
    sub_payloads: list[list[str]] = field(default_factory=list)
    heredocs: list[list[Heredoc]] = field(default_factory=list)  # per sub: the heredocs fed to it
    #: per sub: "shell" or "other" when an interpreter runs a heredoc fed to it as its program, the sub itself
    #: (``bash <<EOF``) or a later stage of its pipeline (``cat <<EOF | sh``); "" otherwise
    code_heredoc: list[str] = field(default_factory=list)
    #: per sub: the ``ssh host`` stages of its pipeline that run a heredoc fed to it as a script on that host
    remote_heredoc: list[list[str]] = field(default_factory=list)
    open_heredoc: bool = False  # a heredoc with no line after it to start its body
    open_bracket: bool = False  # a ${, [, a=( ... still open at the end (see _scan)
    children: list[Parsed] = field(default_factory=list)  # the parsed payloads


_LINE_END = re.compile(r"[ \t]*(?:\n|$)")

#: a cap on the total characters scanned to resolve ``((...))`` arithmetic ends in one command line; past it an
#: open ``((`` is left unresolved (the command is unterminated, a person confirms) instead of rescanning quadratically
_ARITH_SCAN_BUDGET = 100_000


def _match_paren(text: str, open_idx: int, arith: bool | None = None, budget: list[int] | None = None) -> int:
    """Index of the ``)`` that closes the ``(`` at ``open_idx`` (quote, escape, comment and heredoc aware); -1 when
    unbalanced. In arithmetic (``arith``; by default when the text there is ``((``) a ``<<`` is a shift and a ``#``
    starts no comment."""
    depth, i, n = 0, open_idx, len(text)
    heredocs: list[HeredocWord] = []
    if arith is None:
        arith = text.startswith("((", open_idx)  # $((...)): a << in it is a shift
    while i < n:
        if budget is not None:
            budget[0] -= 1
            if budget[0] < 0:
                return -2  # scanned too far: the caller treats the ((...)) as unresolved
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return -1
            i = j + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            if i >= n:
                return -1
            i += 1
            continue
        if text.startswith("<<<", i):
            i += 3
            continue
        if text.startswith("<<", i) and not arith:
            i, heredoc = _heredoc_word(text, i)
            if heredoc is None:
                return -1  # where the body ends is unknown
            heredocs.append(heredoc)
            continue
        if c == "\n" and heredocs:
            i = _skip_heredoc_bodies(text, i + 1, heredocs)
            continue
        if c == "#" and not arith and i > open_idx and text[i - 1] in " \t\n;&|()<>":
            eol = text.find("\n", i)
            i = n if eol < 0 else eol
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _arith_end(text: str, i: int, budget: list[int] | None = None) -> int:
    """Index of the last ``)`` of the arithmetic command ``((...))`` at ``i``, where ``<<`` is a shift, or -1 when it
    is not one: as in bash, the inner ``(`` must close right before a ``)`` (``((a) )`` is two subshells)."""
    inner = _match_paren(text, i + 1, arith=True, budget=budget)
    if inner == -2:
        return -2
    return inner + 1 if inner > 0 and text.startswith(")", inner + 1) else -1


def _scan(text: str) -> Parsed:
    """Quote-aware split into simple commands and pipelines, plus the payloads of the substitutions found on the way.
    ``#`` starts a comment only at the start of a word outside quotes; ``\\<newline>`` joins lines; ``<<`` starts a
    heredoc only outside quotes and outside an open ``((...))``, ``${...}``, ``$[...]``, ``[...]`` or ``a=(...)``,
    however far back it opened (its body is not commands; the substitutions in an unquoted body are payloads of the
    command it is fed to). In ``((...))`` a ``<<`` is a shift; one that bash may not read as a heredoc, or whose body
    end bash may see elsewhere (in the other brackets, an odd delimiter word), makes the command unterminated and the
    next lines stay commands."""
    out = Parsed()
    cur: list[str] = []
    cur_payloads: list[str] = []
    clean: list[str] = []
    pipe: list[str] = []
    heredocs: list[HeredocWord] = []  # the heredocs started on this line
    heredoc_info: list[tuple[bool, int]] = []  # per delimiter: quoted, index of the sub it is fed to
    # the open brackets, innermost last: "((" (with the index of its last ")"), "${", "{" (inside "${"), "[" (also for
    # "$["), "=(" and "=(!" (an a=(...) with an operator in it), "$(" (a substitution whose payload ends in an open
    # bracket); the last two never close. A closer that does not match the innermost is ignored, so an unsure bracket
    # stays open: a << then needs a person instead of hiding the next lines
    ctx: list[tuple[str, int]] = []
    i, n = 0, len(text)
    arith_budget = [_ARITH_SCAN_BUDGET]

    def emit(s: str) -> None:
        cur.append(s)
        clean.append(s)

    def flush(sep: str, piped: bool = False) -> None:
        nonlocal pipe
        part = "".join(cur).strip()
        if part:
            out.subs.append(part)
            out.sub_payloads.append(list(cur_payloads))
            out.heredocs.append([])
            pipe.append(part)
        elif piped and not pipe and out.pipelines:
            pipe = out.pipelines.pop()  # `(a) | b`: the subshell feeds b
        cur.clear()
        cur_payloads.clear()
        if not piped and pipe:
            out.pipelines.append(pipe)
            pipe = []
        clean.append(sep)

    def subst(at: int, open_len: int) -> tuple[int, str]:
        """``$(``, ``<(`` or ``>(`` at ``at``: (index after the closing paren, cleaned text)."""
        end = _match_paren(text, at + open_len - 1)
        if end < 0:
            out.unterminated = True
            end = n
        payload = _subst_payload(text, at, end)
        cur_payloads.append(payload)
        inner = _scan(payload)
        # `$(cat <<EOF)`: bash reads the body from the lines after the substitution
        out.unterminated = out.unterminated or inner.open_heredoc
        if inner.open_bracket:
            # `$(a[)...`: bash reads on past a ) in a bracket, so where the substitution ends is unsure
            out.unterminated = True
            ctx.append(("$(", -1))
        return end + 1, text[at : at + open_len] + inner.clean.strip() + ")"

    def backtick(at: int) -> tuple[int, str]:
        end = text.find("`", at + 1)
        if end < 0:
            out.unterminated = True
            end = n
        cur_payloads.append(text[at + 1 : end])
        out.unterminated = out.unterminated or _scan(text[at + 1 : end]).open_heredoc
        return end + 1, text[at : end + 1]

    while i < n:
        while ctx and ctx[-1][0] == "((" and i > ctx[-1][1]:
            ctx.pop()  # past the )) of an arithmetic command
        c = text[i]
        if ctx and ctx[-1][0] == "=(" and (c in ";|&" or (c in "<>" and text[i + 1 : i + 2] != "(")):
            ctx[-1] = ("=(!", -1)  # an operator in a=(...): bash drops the rest of the line, heredocs included
        if c == "\\" and i + 1 < n:
            if text[i + 1] != "\n":  # \<newline> is a line continuation: both go
                emit(text[i : i + 2])
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                out.unterminated = True
                j = n - 1
            emit(text[i : j + 1])
            i = j + 1
        elif c == '"':
            piece, j, closed = ['"'], i + 1, False
            while j < n:
                d = text[j]
                if d == '"':
                    closed, j = True, j + 1
                    break
                if d == "\\" and j + 1 < n:
                    if text[j + 1] != "\n":
                        piece.append(text[j : j + 2])
                    j += 2
                elif text.startswith("$(", j):
                    j, s = subst(j, 2)
                    piece.append(s)
                elif d == "`":
                    j, s = backtick(j)
                    piece.append(s)
                else:
                    piece.append(d)
                    j += 1
            if closed:
                piece.append('"')
            else:
                out.unterminated = True
            emit("".join(piece))
            i = j
        elif c == "#" and (not cur or (len(cur[-1]) == 1 and cur[-1] in " \t<>&")):
            eol = text.find("\n", i)
            eol = n if eol < 0 else eol
            if any(q in text[i:eol] for q in "'\"`"):
                out.comment_quote = True
            i = eol
        elif text.startswith(("$(", "<(", ">("), i):
            i, s = subst(i, 2)
            emit(s)
        elif text.startswith(("${", "$["), i):
            ctx.append(("${" if text[i + 1] == "{" else "[", -1))
            emit(text[i : i + 2])
            i += 2
        elif c in "[]{}":
            top = ctx[-1][0] if ctx else ""
            if c == "[" or (c == "{" and top in ("${", "{")):  # a bare { opens nothing outside ${...}
                ctx.append((c, -1))
            elif (c == "]" and top == "[") or (c == "}" and top in ("${", "{")):
                ctx.pop()
            emit(c)
            i += 1
        elif c == "`":
            i, s = backtick(i)
            emit(s)
        elif text.startswith("<<<", i):
            emit("<<<")
            i += 3
        elif text.startswith("<<", i) and ctx:
            # in ((...)) a shift; in the other brackets bash reads no heredoc either (or fails on the line), but where
            # its word or line ends is unsure, and so is a shift at the end of a line (a heredoc with no word outside)
            if any(kind != "((" for kind, _ in ctx) or _LINE_END.match(text, i + 2):
                out.unterminated = True
            emit("<<")
            i += 2
        elif text.startswith("<<", i):
            end, heredoc = _heredoc_word(text, i)
            if heredoc:
                heredocs.append(heredoc)
                heredoc_info.append((heredoc[2], len(out.subs)))  # the sub being built gets this index
                emit(text[i:end])
                i = end
            else:
                out.unterminated = True
                emit("<<")
                i += 2
        elif c == "\n":
            flush("\n")
            if heredocs and ctx:
                # `cat <<EOF ${x:-<newline>...}`: bash reads the body after the line the bracket closes on; the next
                # lines stay commands
                out.unterminated = True
                heredocs.clear()
                heredoc_info.clear()
            bodies: list[str] = []
            i = _skip_heredoc_bodies(text, i + 1, heredocs, bodies)
            for (quoted, k), body in zip(heredoc_info, bodies, strict=True):
                payloads, open_ = ([], False) if quoted else _body_payloads(body)
                out.heredocs[k].append(Heredoc(body, quoted, payloads))
                out.sub_payloads[k].extend(payloads)
                out.unterminated = out.unterminated or open_
            heredoc_info.clear()
        elif text[i : i + 2] in ("&&", "||"):
            flush(text[i : i + 2])
            i += 2
        elif text[i : i + 2] == "|&":
            flush("|&", piped=True)
            i += 2
        elif c == "|":
            flush("|", piped=True)
            i += 1
        elif c == ";":
            flush(";")
            i += 1
        elif c == "&":
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if prev in "<>" or nxt == ">":  # 2>&1, >&2, &>file: part of a redirection
                emit(c)
            else:
                flush("&")  # a lone & backgrounds the command; what follows is another command
            i += 1
        elif c in "()":
            top = ctx[-1][0] if ctx else ""
            if c == "(":
                if top == "=(":
                    ctx[-1] = ("=(!", -1)
                elif cur and cur[-1] == "=":
                    ctx.append(("=(", -1))  # a=(...), also a=((...)): no arithmetic
                elif top != "((" and text.startswith("((", i):
                    end = _arith_end(text, i, arith_budget)
                    if end == -2:  # too long to resolve: an open (( that never closes, so the command is unterminated
                        out.unterminated = True
                        ctx.append(("((", n))
                    elif end > 0:
                        ctx.append(("((", end))
            elif top == "=(":
                ctx.pop()
            flush(c)
            i += 1
        else:
            emit(c)
            i += 1
    flush("")
    out.open_heredoc = bool(heredocs)
    out.open_bracket = any(kind != "((" or end >= n for kind, end in ctx)
    out.clean = "".join(clean)
    out.code_heredoc, out.remote_heredoc = _code_heredocs(out)
    return out


_REDIRECT_WORD = re.compile(r"(?:\d*|&)(?:<<?<?|>>?|<>|[<>]&)")


def _without_redirects(words: list[str]) -> list[str]:
    """``words`` without the redirections (``<<EOF``, ``2>&1``, ``> out``, ``<<< text``): a redirect is not the script
    operand of ``bash <<EOF``."""
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        m = _REDIRECT_WORD.match(word)
        if m:
            skip = m.end() == len(word)  # a lone operator takes the next word as its target
        else:
            out.append(word)
    return out


def _ssh_command(args: list[str]) -> list[str]:
    """The remote command of ``ssh`` with arguments ``args`` ([] when there is none). Like ssh: options also after the
    host name, ``--`` ends them."""
    host, i = False, 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            return args[i:] if host else args[i + 1 :]
        if a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch in SSH_VALUE_OPTS["ssh"]:
                    i += 0 if a[k + 1 :] else 1
                    break
        elif host:
            return args[i - 1 :]
        else:
            host = True
    return []


def _stdin_program(stage: str) -> str:
    """ "shell" or "other" when ``stage`` is an interpreter that executes its stdin as a program (``bash``,
    ``python3 -``, ``sudo sh -s``, ``ssh host`` with no command: the remote login shell), else ""."""
    argv = normalize_argv(_without_redirects(_tokens(stage)))
    if argv[:1] == ["ssh"]:
        remote = _ssh_command(argv[1:])
        return _stdin_program(" ".join(remote)) if remote else "shell"
    if not (argv and _is_interpreter(argv[0]) and _runs_stdin_as_code(argv)):
        return ""
    return "shell" if argv[0] in _SHELLS or argv[0] in ("source", ".") else "other"


def _code_heredocs(parsed: Parsed) -> tuple[list[str], list[list[str]]]:
    """Per sub: what runs a heredoc fed to it as a program, the sub itself or a later stage of its pipeline, and the
    ssh stages among them (see Parsed.code_heredoc, Parsed.remote_heredoc)."""
    out = [""] * len(parsed.subs)
    remote: list[list[str]] = [[] for _ in parsed.subs]
    if not any(parsed.heredocs):
        return out, remote
    base = 0  # the pipelines list the subs in order
    for stages in parsed.pipelines:
        runs = [_stdin_program(s) for s in stages]
        argvs = [normalize_argv(_without_redirects(_tokens(s))) for s in stages]
        ssh = [shlex.join(a) if a[:1] == ["ssh"] and r == "shell" else "" for a, r in zip(argvs, runs, strict=True)]
        for k in range(len(stages)):
            if parsed.heredocs[base + k]:
                out[base + k] = "shell" if "shell" in runs[k:] else next((r for r in runs[k:] if r), "")
                remote[base + k] = [s for s in ssh[k:] if s]
        base += len(stages)
    return out, remote


def _launcher_payloads(sub: str) -> list[str]:
    """Quoted arguments of ``bash -c``, ``ssh host "..."``, ``sudo sh -c ...``, the joined arguments of ``eval`` and
    the values of ``ssh -oProxyCommand=...`` (see ssh_option_commands): strings run as commands."""
    toks = _tokens(sub)
    out: list[str] = []
    pre = _strip_wrappers(toks)
    if len(pre) > 1 and os.path.basename(pre[0]) == "eval":
        out.append(" ".join(pre[1:]))
    real = normalize_argv(toks)
    if real and real[0] in LAUNCHERS:
        out += [t for t in real[1:] if not t.startswith("-") and re.search(r"\s|[;&|$`<>()]", t)]
    if real:
        out += ssh_option_commands(real[0], real[1:])
    return out


def parse(command: str, _depth: int = 0) -> Parsed:
    """Split ``command`` into simple commands and collect every command nested inside it (recursively)."""
    parsed = _scan(command)
    if _depth >= 6:  # pathological nesting is cut off; the caller still checks the cleaned text
        return parsed
    payloads = [p for ps in parsed.sub_payloads for p in ps]
    for sub in parsed.subs:
        payloads.extend(_launcher_payloads(sub))
    for payload in payloads:
        inner = parse(payload, _depth + 1)
        parsed.children.append(inner)
        parsed.nested.extend([*inner.subs, *inner.nested])
        parsed.unterminated = parsed.unterminated or inner.unterminated
        parsed.comment_quote = parsed.comment_quote or inner.comment_quote
    # a heredoc a shell runs is its script (`bash <<'EOF'`, `cat <<EOF | sh`): checked like `bash -c "..."`, except for
    # its quotes (a stray one in a comment of a long script is not worth a prompt). The program of another
    # interpreter is not shell: such a heredoc is only kept out of the allowlist (see engine._decide_bash).
    for k, bodies in enumerate(parsed.heredocs):
        for heredoc in bodies if parsed.code_heredoc[k] == "shell" else ():
            inner = parse(heredoc.body, _depth + 1)
            parsed.children.append(inner)
            parsed.nested.extend([*inner.subs, *inner.nested])
            # `ssh host <<EOF` runs the body on host: each command is also checked as `ssh host '<command>'`, the
            # spelling the rules about remote hosts match
            remote = [*inner.subs, *inner.nested]
            parsed.children += [_scan(f"{ssh} {shlex.quote(c)}") for ssh in parsed.remote_heredoc[k] for c in remote]
    return parsed


def split_commands(command: str) -> list[str]:
    """Split a shell command into simple commands (quote-aware: ``&&``, ``||``, ``;``, ``|``, ``&``, ``|&``,
    newlines and subshell parentheses split; quoted text, substitutions and heredoc bodies stay together)."""
    return parse(command).subs


def command_prefix(sub_command: str) -> str:
    """Narrowest arity prefix string for an ``always`` rule suggestion.

    ``git commit -m x`` -> ``"git commit"``; unknown commands -> first token.
    Flags never count: they are stripped before arity lookup, except python's ``-m MODULE``
    (``python -m pytest -q`` -> ``"python -m pytest"``, not ``"python pytest"``).
    """
    try:
        tokens = shlex.split(sub_command, posix=True)
    except ValueError:
        tokens = sub_command.split()
    while tokens and "=" in tokens[0] and tokens[0].split("=", 1)[0].isidentifier():
        tokens = tokens[1:]  # a leading VAR=value is not the command, and is often a secret
    python = bool(tokens) and tokens[0].startswith("python")
    tokens = [t for t in tokens if not t.startswith("-") or t in ("-", "--") or (python and t == "-m")]
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
