"""Hardline denies: commands that are never allowed, in any mode.

Checked before any rule evaluation (and before modes: even ``yolo`` refuses
them). Extendable from config via ``permissions: {hardline: [...]}``.

The command line is first taken apart with a small quote-aware shell parser (:func:`parse`): simple commands are
split on ``&&  ||  ;  |  &  |&``, newlines and subshell parentheses, and everything nested inside them (``$(...)``,
backticks, ``bash -c "..."``, ``eval``, ``ssh host "..."``, a script fed to a shell by a heredoc or an ``echo``) is
collected, so a denied command cannot hide behind a lone ``&``, a quote, a substitution, ``then``/``{``/``!`` or a
wrapper. Shell variables the line assigns are put in (``a=rm; $a -rf /``), ``rm`` targets are resolved against the
directory the line is in, and what cannot be checked (:func:`needs_human`) goes to a person.
"""

from __future__ import annotations

import codecs
import contextlib
import fnmatch
import functools
import logging
import os
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

#: (name, regex) pairs matched against the raw command line and every simple command.
HARDLINE_PATTERNS: list[tuple[str, str]] = [
    # rm -rf / and ~ are caught by _rm_check (token based: any flag spelling, quotes, sudo/env wrappers)
    ("mkfs", r"\bmkfs(?:\.\w+)?\b"),  # mkfs / mkfs.ext4 ...
    ("dd-to-device", r"\bdd\b.*\bof=/dev/"),  # dd of=/dev/...
    ("pipe-to-shell", r"\bcurl\b[^|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b"),  # curl ... | sh
    ("wget-pipe-to-shell", r"\bwget\b[^|]*-O\s*-\s*[^|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b"),
    # env-dump (env / printenv printing the environment) is token based: see _env_dump_check
    # (?i): drvfs (/mnt/c) and macOS open ~/.ssh for ~/.SSH and .env for .ENV
    ("ssh-key-cat", r"(?i)\bcat\b[^\n;|&]*\.ssh/"),  # cat ~/.ssh/...
    # cat .env / *.env, but not the committed templates (.env.example ...)
    ("dotenv-cat", r"(?i)\bcat\b[^\n;|&]*\.env\b(?!\.(?:example|sample|template|dist)\b)"),
    ("git-push-force-main", r"\bgit\b[^\n;|&]*\bpush\b(?=[^\n;|&]*--force)(?=[^\n;|&]*\b(?:main|master)\b)"),
    # `function f { f|f& }; f`: a function that pipes itself into itself (`f(){ f|f& }` is _fork_bomb's)
    ("fork-bomb", r"\bfunction\s+([\w:.-]+)\s*(?:\(\)\s*)?\{\s*\1\s*\|\s*\1\s*(?:&\s*)?\}"),
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
    "block-device-write",  # a redirect, cp/mv/tee/shred/wipefs ... onto /dev/sd*, nvme*, vd*, hd*, mmcblk*, disk*
    "recursive-chmod-root",  # chmod/chown/chgrp -R on / (_recursive_root_check)
    "find-delete-root",  # find / ... -delete (_find_check)
    "nesting-too-deep",  # yolo only: nesting past the parse limit cannot be checked, and yolo has nobody to ask
    "env-dump",  # env / printenv / export -p / set printing the environment (_env_dump_check)
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
        "busybox",
        "flock",
        "strace",
        "ltrace",
        "unshare",
        "chroot",
        "setpriv",
        "fakeroot",
        "nsenter",
        "gosu",
        "su-exec",
        "pkexec",
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
    body and the script a heredoc, a here-string or an ``echo`` feeds to a shell). The structural checks (rm targets,
    git push, environment dumps, downloads fed to a shell, block devices, ``find / -delete``) run on the normalised
    argv and the pipelines. ``cwd`` and ``home`` expand ``$PWD``, ``~`` and ``$HOME`` in rm targets and resolve
    relative ones (also after a ``cd``); home defaults to the real one.
    """
    home = home if home is not None else os.path.expanduser("~")
    parsed = parse(command)
    return _match(parsed.clean, extra_patterns) or _check_parsed(
        parsed, extra_patterns, cwd, home, _scope(parsed, cwd, home)
    )


def _check_parsed(parsed: Parsed, extra: list[str] | None, cwd: str | None, home: str, scope: _Scope) -> str | None:
    for k, sub in enumerate(parsed.subs):
        hit = _match(sub, extra) or _simple_check(sub) or _device_redirect_check(sub)
        for text in expand_assigned(sub, scope.env):  # the regexes see the command as written (values put in)
            for legacy in (False, True):
                hit = hit or _match(" ".join(normalize_argv(_tokens(text), legacy=legacy)), extra)
        for argv in _argvs(sub, scope.env):
            hit = hit or (
                _rm_check(argv, cwd, home, scope.dirs)
                or _git_push_check(argv)
                or _env_dump_check(argv)
                or _fetch_run_check(argv, parsed.sub_payloads[k])
                or _device_check(argv)
                or _recursive_root_check(argv, cwd, home, scope.dirs)
                or _find_check(argv, cwd, home, scope.dirs)
            )
        if hit:
            return hit
    hit = _pipeline_check(parsed.pipelines, cwd, home, scope.dirs)
    if hit:
        return hit
    for child in parsed.children:
        hit = _match(child.clean, extra) or _check_parsed(child, extra, cwd, home, scope)
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


@functools.lru_cache(maxsize=256)
def _user_pattern(rx: str) -> re.Pattern[str]:
    """A ``permissions.hardline`` entry from the user's config. A malformed regex must not make every bash decision
    raise (a crash is not a deny): it is logged once and matched as the literal text it was written as, so it still
    denies the command line it was meant for."""
    try:
        return re.compile(rx)
    except (re.error, RecursionError) as e:
        _log.warning("invalid regex in permissions.hardline (%s): %r; matching it as literal text instead", e, rx)
        return re.compile(re.escape(rx))


#: ``(){ f|f& }``: the body of a fork bomb, from its ``()`` on (the name in front is checked by _fork_bomb).
_FORK_BODY = re.compile(r"\(\)\s*\{\s*([\w:.-]+)\s*\|\s*\1\s*(?:&\s*)?\}")  # no two \s* side by side


def _fork_bomb(text: str) -> bool:
    """``:(){ :|:& };:`` / ``f(){ f|f& }``: a function whose body pipes its own name into itself. Matched from each
    ``()`` and checked back to the word in front, which must end with that name: linear in the line, where one regex
    from the name on tried every start of a long word (quadratic, seconds for a 10k-character command)."""
    for m in _FORK_BODY.finditer(text):
        end = m.start()
        while end and text[end - 1].isspace():
            end -= 1
        if text.endswith(m.group(1), 0, end):
            return True
    return False


def _match(text: str, extra_patterns: list[str] | None) -> str | None:
    for name, rx in _COMPILED:
        if rx.search(text):
            return name
    if _fork_bomb(text):
        return "fork-bomb"
    for i, rx in enumerate(extra_patterns or []):
        if _user_pattern(str(rx)).search(text):
            return f"config-hardline-{i}"
    return None


def _simple_check(sub: str) -> str | None:
    for name, rx in _COMPILED_SIMPLE:
        if rx.search(sub):
            return name
    return None


#: ``$'rm'``, ``$'\\x72m'``: ANSI-C quoting, which shlex does not know (it would read ``$rm``).
_ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'")


def _ansi_c(m: re.Match[str]) -> str:
    return shlex.quote(_unescape(m.group(1)))


def decode_ansi_c(text: str) -> str:
    """``text`` with its ``$'...'`` words decoded (``$'\\x72m'`` is ``rm``)."""
    return _ANSI_C.sub(_ansi_c, text)


def _tokens(sub: str) -> list[str]:
    try:
        return shlex.split(sub, posix=True)
    except ValueError:
        return sub.split()


#: Shell reserved words and braces that start a simple command without being one: ``then rm -rf /``, ``{ rm x``,
#: ``! rm x``. ``for``/``select``/``case`` open a header (``for i in a b``) that runs nothing itself.
_RESERVED = frozenset({"if", "then", "elif", "else", "fi", "while", "until", "do", "done", "esac", "{", "}", "!"})
_HEADERS = frozenset({"for", "select", "case"})
#: Every word that is shell syntax at the start of a command (no rule is ever proposed for one).
RESERVED_WORDS = _RESERVED | _HEADERS | {"function", "coproc"}


def _strip_reserved(tokens: list[str]) -> list[str]:
    """``tokens`` without the reserved words / braces / function and coproc heads in front of the command; a loop or
    ``case`` header is no command at all (empty)."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in _HEADERS:
            return []
        if t == "function":  # function NAME [{ ...]
            i += 2
        elif t == "coproc":  # coproc NAME { ...; }   or   coproc cmd ...
            i += 3 if tokens[i + 2 : i + 3] == ["{"] else 1
        elif t in _RESERVED:
            i += 1
        else:
            break
    return tokens[i:]


_LEADING_RESERVED = re.compile(r"^(?:(?:if|then|elif|else|fi|while|until|do|done|esac|\{|\}|!)(?:\s+|$))+")


def strip_leading_reserved(sub: str) -> str:
    """``sub`` text without the reserved words / braces in front of the command (``then git push`` is ``git push``)."""
    return _LEADING_RESERVED.sub("", sub).strip() or sub


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Drop leading sudo/env/nohup/... (with their VAR=x, -flag and numeric arguments) to reach the real command."""
    tokens = _strip_reserved(tokens)
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
    "env": ("uSCPa", frozenset({"--unset", "--split-string", "--chdir", "--argv0"}), 0),
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
    "busybox": ("", frozenset(), 0),  # busybox rm -rf x
    "flock": ("wEc", frozenset({"--timeout", "--conflict-exit-code", "--command"}), 1),  # flock [opts] FILE cmd
    "strace": (
        "aebEIoOpPsSuUX",
        frozenset({"--output", "--trace", "--env", "--signal", "--status", "--inject", "--fault", "--string-limit"}),
        0,
    ),
    "ltrace": ("aAeFlnopsuwx", frozenset(), 0),
    "watch": ("n", frozenset({"--interval"}), 0),
    "unshare": (
        "SGRw",
        frozenset(
            {
                "--map-user",
                "--map-group",
                "--map-users",
                "--map-groups",
                "--propagation",
                "--setgroups",
                "--root",
                "--wd",
            }
        ),
        0,
    ),
    "chroot": ("", frozenset({"--userspec", "--groups"}), 1),  # chroot [opts] NEWROOT cmd
    "setpriv": (
        "",
        frozenset(
            {
                "--reuid",
                "--regid",
                "--ruid",
                "--euid",
                "--rgid",
                "--egid",
                "--groups",
                "--inh-caps",
                "--ambient-caps",
                "--bounding-set",
                "--securebits",
                "--pdeathsig",
                "--selinux-label",
                "--apparmor-profile",
                "--landlock-access",
                "--landlock-rule",
            }
        ),
        0,
    ),
    "fakeroot": ("lisb", frozenset({"--lib", "--faked"}), 0),
    "nsenter": ("tSG", frozenset({"--target", "--setuid", "--setgid"}), 0),
    "gosu": ("", frozenset(), 1),  # gosu USER cmd
    "su-exec": ("", frozenset(), 1),
    "pkexec": ("", frozenset({"--user"}), 0),
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


def _long_option(key: str, names: frozenset[str]) -> str:
    """``key`` (``--repl``) as GNU getopt reads it: the one of ``names`` it abbreviates (``--replace``), else itself."""
    if key in names or len(key) < 3:
        return key
    hits = [n for n in names if n.startswith(key)]
    return hits[0] if len(hits) == 1 else key


def _skip_wrapper_options(
    name: str,
    args: list[str],
    spec: tuple[str, frozenset[str], int],
    reads: list[str] | None = None,
    legacy: bool = False,
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
            if not legacy:
                key = _long_option(key, long_val)  # `sudo --us root rm` is `sudo --user root rm`
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


def _env_prints(rest: list[str]) -> bool:
    """``env`` with only assignments left to run (``env -- A=1``, ``env -S A=1``), or an expansion where the command
    goes (``env $X``, ``env A=1 $(cmd)``, ``env -- A=1 $X``: empty, it prints too), prints the environment: it stays
    the command. (After ``--`` or ``-S`` the words may start with assignments: the command is the first other word.)"""
    command = next((t for t in rest if not _is_assignment(t)), None)
    return command is None or UNRESOLVED.search(command) is not None


#: Wrappers added by the second audit. ``normalize_argv(..., legacy=True)`` leaves them (and the reserved words) alone:
#: the engine's credential scan and rule forms look at both spellings, so teaching the normaliser more never lets a
#: command through that an older reading stopped.
_NEW_WRAPPERS = frozenset(
    {"busybox", "flock", "strace", "ltrace", "watch", "unshare", "chroot", "setpriv", "fakeroot", "nsenter"}
    | {"gosu", "su-exec", "pkexec"}
)


def normalize_argv(tokens: list[str], reads: list[str] | None = None, *, legacy: bool = False) -> list[str]:
    """The command that really runs: leading ``VAR=x`` assignments and wrappers (sudo, env, nice, timeout, xargs,
    ...) with their options dropped, ``eval`` arguments re-split, git's global options (``-C dir``, ``-c k=v``, ...)
    removed and argv0 reduced to its basename. A wrapper with nothing left to run is itself the command (``env``).
    Reserved words and braces in front (``then rm``, ``{ rm``) go too. ``reads`` collects the files a dropped wrapper
    option reads (``xargs -a FILE``, see _WRAPPER_READ_OPTS). ``legacy``: as before the audit (no reserved words,
    busybox / flock / strace / watch / unshare / chroot ... are not wrappers, and a long option must be spelled out)."""
    argv = list(tokens)
    for _ in range(len(tokens) + 1):  # every round drops at least one token
        while argv and _is_assignment(argv[0]):
            argv = argv[1:]
        if not legacy:
            argv = _strip_reserved(argv)
        if not argv:
            return []
        if _is_assignment(argv[0]):  # `then FOO=1 cmd`
            continue
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
        if spec is None or (legacy and name in _NEW_WRAPPERS):
            return argv
        rest = _skip_wrapper_options(name, argv[1:], spec, reads, legacy)
        if not rest or (name == "env" and _env_prints(rest)):
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


def _xargs_replace(tokens: list[str]) -> str:
    """The replace string of the ``xargs`` in ``tokens`` (``-I TOK``, ``-ITOK``, ``-i[TOK]``, ``--replace[=TOK]``; the
    last two default to ``{}``): the word its command line repeats once per input item. "" when it has none."""
    short_val, long_val, _ = _WRAPPER_SPECS["xargs"]
    start = next((k for k, t in enumerate(tokens) if os.path.basename(t) == "xargs"), len(tokens))
    token = ""
    i = start + 1
    while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "--":  # the options end at the command
        a = tokens[i]
        i += 1
        if a.startswith("--"):
            key, eq, value = a.partition("=")
            key = _long_option(key, long_val | {"--replace"})
            if key == "--replace":
                token = value or "{}"
            elif key in long_val and not eq:
                i += 1
            continue
        for k, ch in enumerate(a[1:], 1):
            if ch == "i":
                token = a[k + 1 :] or "{}"
                break
            if ch in short_val:
                value = a[k + 1 :]
                if not value and i < len(tokens):
                    value, i = tokens[i], i + 1
                if ch == "I":
                    token = value
                break
    return token


def _pipeline_check(
    pipelines: list[list[str]], cwd: str | None = None, home: str = "", dirs: list[str] | None = None
) -> str | None:
    """A download fed into a later stage of the same pipeline that runs stdin as code (``curl x | sudo -n sh``,
    ``curl x | python3 -``); ``curl x | python3 -m json.tool`` only reads it. And a list of paths fed to a recursive
    ``rm`` through ``xargs`` (``echo ~ | xargs rm -rf``, ``find ~ | xargs -I{} rm -r {}``)."""
    for stages in pipelines:
        fetched = False
        previous: list[str] = []
        for stage in stages:
            tokens = _tokens(stage)
            argv = normalize_argv(tokens)
            name = argv[0] if argv else ""
            if fetched and _is_interpreter(name) and _runs_stdin_as_code(argv):
                return "pipe-to-shell"
            if previous and name == "rm":
                hit = _fed_rm_check(previous, argv, cwd, home, dirs or [], _xargs_replace(tokens))
                if hit:
                    return hit
            fetched = fetched or name in _FETCHERS
            previous = argv
    return None


def _fed_rm_check(
    source: list[str], rm: list[str], cwd: str | None, home: str, dirs: list[str], replace: str = ""
) -> str | None:
    """``rm -r`` with no operands of its own, run on what the previous stage printed (``xargs rm -rf``): the paths an
    ``echo``/``printf`` was given, or the start points of a ``find`` with no test that picks files, are its targets.
    With a replace string (``xargs -I{} rm -rf {}``) they go in where the operands hold it."""
    recursive, targets = _rm_split(rm[1:])
    if not recursive or (targets and not (replace and any(replace in t for t in targets))):
        return None
    if source[0] in ("echo", "printf"):
        fed = [a for a in source[1:] if not a.startswith("-")]
    elif source[0] == "find" and not _find_picks_some(source[1:]):
        fed = _find_roots(source[1:])
    else:
        return None
    for operands in [[t.replace(replace, f) for t in targets] for f in fed] if targets else [fed]:
        hit = _rm_check(["rm", "-rf", *operands], cwd, home, dirs)
        if hit:
            return hit
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


def _push_parts(args: list[str]) -> tuple[bool, list[str]]:
    """(a force flag is present, the positional arguments: remote then refspecs) of ``git push`` ``args``."""
    force = False
    positional: list[str] = []
    for a in args:
        long_force = a.split("=", 1)[0] in ("--force", "--force-with-lease", "--force-if-includes")
        if long_force or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]):
            force = True
        elif not a.startswith("-"):
            positional.append(a)
    return force, positional


def _git_push_check(argv: list[str]) -> str | None:
    """``git push`` with a force flag or a ``+refspec`` onto main/master, or ``--mirror``. A force push without a
    refspec (the upstream, unknown here) is not a hardline deny: see :func:`risky`."""
    if argv[:2] != ["git", "push"]:
        return None
    args = argv[2:]
    if "--mirror" in args:
        return "git-push-mirror"
    force, positional = _push_parts(args)
    for refspec in positional[1:]:
        if refspec.lstrip("+").split(":")[-1] in _MAIN_REFS and (force or refspec.startswith("+")):
            return "git-push-force-main"
    return None


#: Variables that hold, or may hold, a credential: redact.py's key / token / secret / password / credential words, a
#: whole-word PASS and the URL / URI / DSN a password is often written into. ``printenv`` does not print them.
_SECRET_ENV_NAME = re.compile(
    r"(?i)key|token|secret|pass(?:w|ph)|pwd|credential|authoriz|cookie|conn(?:ection)?[_-]?str"
    r"|(?<![a-z])(?:pass|auth|url|uri|dsn)(?![a-z])"
)
#: A ``printenv`` argument that is no name but an expansion or a glob (its value is not known here).
_ENV_NAME_EXPANSION = re.compile(r"[$`*?\[\]{}~\\]")


def _env_dump_check(argv: list[str]) -> str | None:
    """``env`` / ``printenv`` / ``export -p`` / ``set`` printing the environment (secrets) to the transcript. ``env``
    only gets here when it has no command to run (:func:`normalize_argv` drops it otherwise: ``env A=1 cmd`` is
    ``cmd``). ``printenv NAME`` prints one variable: only a plain, non-credential name is let through."""
    if not argv:
        return None
    name, args = argv[0], argv[1:]
    if name == "printenv":
        names = [a for a in args if not a.startswith("-")]
        if names and not any(_ENV_NAME_EXPANSION.search(n) or _SECRET_ENV_NAME.search(n) for n in names):
            return None
        return "env-dump"
    if name == "env" or (name == "export" and set(args) <= {"-p"}) or (name == "set" and not args):
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


# ── redirections (the engine shares these) ──

#: Redirections that are harmless next to any command (they do not write or read an arbitrary file).
HARMLESS_REDIRECT = re.compile(r"(?:\d*[<>]&(?:\d+|-)|&>>?\s*/dev/null|\d*>>?&?\s*/dev/null)(?![^\s;&|<>()])")
#: ``>f``, ``>>f``, ``>|f`` (clobber), ``<f``, ``<>f`` (opens read-write: creates and overwrites in place), ``&>f``,
#: ``&>>f``, ``>&f`` (the last three send stdout+stderr to f).
REDIRECT = re.compile(r"(?:^|[^<>&\d])(?:&>>?|\d*(?:<>|>\||>>?|<)&?)\s*([^\s;&|<>()]+)")
#: The redirections that write: ``>``, ``>>``, ``>|``, ``<>``, ``&>``, ``&>>``, ``>&``. Reading with ``<`` is never
#: restricted by the write roots.
WRITE_REDIRECT = re.compile(r"(?:^|[^<>&\d])(?:&>>?|\d*(?:<>|>\||>>?)&?)\s*([^\s;&|<>()]+)")
#: ``<<EOF``, ``<<-'EOF'``, ``<<< word``: the operator with its delimiter or word.
HEREDOC_OPS = re.compile(r"<<<?-?[ \t]*(?:'[^']*'|\"[^\"]*\"|\\?[^\s;&|<>()]+)")


#: The redirection pattern as it was before ``<>`` and ``>|`` were known.
_LEGACY_REDIRECT = re.compile(r"(?:^|[^<>&\d])(?:&>>?|\d*(?:>>?|<)&?)\s*([^\s;&|<>()]+)")


def strip_redirects(sub: str, heredocs: bool = True, legacy: bool = False) -> str:
    """``sub`` without its redirections (operator and target) and, ``heredocs``, heredoc / here-string operators: what
    is left is the command and its arguments. Redirect targets are checked separately. ``legacy``: only the
    operators known before the audit (the engine's credential scan reads both)."""
    rx = _LEGACY_REDIRECT if legacy else REDIRECT
    return rx.sub(" ", HARMLESS_REDIRECT.sub("", HEREDOC_OPS.sub(" ", sub) if heredocs else sub))


# ── brace expansion, variables the command line assigns, working directories ──

_BRACE = re.compile(r"\{([^{},]*,[^{}]*)\}")  # up to the first comma, then the rest: no backtracking over commas
#: The most spellings the checks look at: words of one brace expansion, combinations of the values the line assigns
#: its variables, directories a cd may lead to. Past one the line is not judged on what was kept (see capped).
BRACE_WORDS_CAP = 64
COMBINATIONS_CAP = 16
DIRS_CAP = 256  # each relative cd doubles the directories (joined to every one so far): eight chained cds fit


def _brace_expand(word: str, limit: int) -> tuple[list[str], bool]:
    """(the words of ``word``, True when the cap or the depth limit dropped some)."""
    out = [word]
    cut = False
    for _ in range(8):
        nxt: list[str] = []
        changed = False
        for w in out:
            m = _BRACE.search(w)
            if not m:
                nxt.append(w)
                continue
            changed = True
            nxt.extend(w[: m.start()] + alt + w[m.end() :] for alt in m.group(1).split(","))
        cut = cut or len(nxt) > limit
        out = nxt[:limit]
        if not changed:
            return out, cut
    return out, cut or any(_BRACE.search(w) for w in out)


def brace_expand(word: str, limit: int = BRACE_WORDS_CAP) -> list[str]:
    """The words the shell makes of ``a{b,c}d`` (``abd``, ``acd``); capped so a pathological word cannot explode."""
    return _brace_expand(word, limit)[0]


def brace_words(tokens: list[str]) -> list[str]:
    """``tokens`` with every ``{a,b}`` word split into the words the shell makes of it (``{rm,-rf,/}`` is three)."""
    out: list[str] = []
    for t in tokens:
        out.extend(brace_expand(t) if "{" in t and "," in t else [t])
    return out


#: A shell expansion whose value is not known here: ``$name``, ``${...}``, ``$(...)``, ``$1``, ``$@``, backticks.
#: (``$$``, ``$?``, ``$#``, ``$!`` and ``$-`` are numbers and flags, never a path or a command.)
UNRESOLVED = re.compile(r"\$[({A-Za-z_0-9@*]|`")
_VAR = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
_ASSIGN_COMMANDS = frozenset({"export", "declare", "typeset", "local", "readonly"})
#: Commands that give a variable a value we cannot read off the command line.
_REASSIGNERS = frozenset({"read", "mapfile", "readarray", "getopts", "unset", "select", "for", "let"})
_APPEND = re.compile(r"([A-Za-z_]\w*)\+=")


def _all_subs(parsed: Parsed, shell_only: bool = False) -> Iterator[str]:
    yield from parsed.subs
    for child in parsed.children:
        if not (shell_only and child.script):
            yield from _all_subs(child, shell_only)


def assignments(parsed: Parsed) -> dict[str, list[str]]:
    """The values a command line gives its variables: NAME -> [values], from ``NAME=value`` words (alone, in front of
    a command, or after export/declare/local/readonly), in the line and everything nested in it. Flow is ignored, so
    every value a name gets counts. A name that is also set some other way (an expansion as its value, ``+=``, read,
    for, getopts, ``printf -v``) stays out: it is unresolved. (What an ``eval`` of an unknown string assigns is not
    known; such an ``eval`` is itself a command nobody can vouch for, see needs_human.)"""
    values: dict[str, list[str]] = {}
    tainted: set[str] = set()
    for sub in _all_subs(parsed):
        toks = _tokens(sub)
        head = os.path.basename(toks[0]) if toks else ""
        if head in _ASSIGN_COMMANDS:
            candidates = [t for t in toks[1:] if not t.startswith("-")]
        else:
            candidates = []
            for t in toks:
                if not _is_assignment(t):
                    break
                candidates.append(t)
        for t in candidates:
            name, _, value = t.partition("=")
            if not name.isidentifier():
                continue
            if "$" in value or "`" in value:
                tainted.add(name)
            else:
                values.setdefault(name, []).append(value)
        if head in _REASSIGNERS or (head == "printf" and "-v" in toks):
            tainted.update(t for t in toks[1:] if t.isidentifier())
        tainted.update(m.group(1) for t in toks for m in [_APPEND.match(t)] if m)
    return {k: v for k, v in values.items() if k not in tainted}


def _expand_assigned(text: str, env: dict[str, list[str]]) -> tuple[list[str], bool]:
    """(the texts of :func:`expand_assigned`, True when the cap dropped combinations)."""
    if not env or "$" not in text:
        return [text], False
    names = sorted({m.group(1) or m.group(2) for m in _VAR.finditer(text)} & env.keys())
    if not names:
        return [text], False
    combos: list[dict[str, str]] = [{}]
    cut = False
    for n in names:
        combos = [{**c, n: v} for c in combos for v in env[n]]
        cut = cut or len(combos) > COMBINATIONS_CAP
        combos = combos[:COMBINATIONS_CAP]
    texts = (_VAR.sub(lambda m, c=c: c.get(m.group(1) or m.group(2), m.group(0)), text) for c in combos)
    return list(dict.fromkeys(texts)), cut


def expand_assigned(text: str, env: dict[str, list[str]]) -> list[str]:
    """``text`` with the variables the command line assigns replaced by their values, one text per combination of
    values (capped). ``[text]`` when it uses none of them. Unassigned variables stay as written."""
    return _expand_assigned(text, env)[0]


def _cut_short(sub: str, env: dict[str, list[str]]) -> bool:
    """Did a cap drop spellings of ``sub`` (combinations of assigned values, or words of one of its brace
    expansions)? Then the checks saw only some of what the shell may run."""
    texts, cut = _expand_assigned(sub, env)
    if cut:
        return True
    for text in texts:
        for spelling in dict.fromkeys((text, decode_ansi_c(text)) if "$'" in text else (text,)):
            for t in (*_tokens(spelling), *_tokens(strip_redirects(spelling))):
                if "{" in t and "," in t and _brace_expand(t, BRACE_WORDS_CAP)[1]:
                    return True
    return False


def _argvs(sub: str, env: dict[str, list[str]]) -> list[list[str]]:
    """The normalised argv of ``sub`` in every spelling that matters: as written, with its redirections and heredoc
    operators removed (``>/dev/null rm -rf /``), brace-expanded (``{rm,-rf,/}``) and, when it uses a variable the
    command line assigns, with the values put in (``a=rm; $a -rf /`` is ``rm -rf /``)."""
    out: list[list[str]] = []
    for text in expand_assigned(sub, env):
        for spelling in dict.fromkeys((text, decode_ansi_c(text)) if "$'" in text else (text,)):
            stripped = _tokens(strip_redirects(spelling))
            for toks in (_tokens(spelling), stripped, brace_words(stripped)):
                argv = normalize_argv(toks)
                if argv and argv not in out:
                    out.append(argv)
    return out


@dataclass
class _Scope:
    """What a whole command line sets up for the commands in it."""

    env: dict[str, list[str]] = field(default_factory=dict)  # NAME -> values it is assigned (see assignments)
    #: where a relative path may resolve: the working directory and every directory a cd/pushd names (the line is not
    #: followed in order, so a directory stays in once it was named)
    dirs: list[str] = field(default_factory=list)
    cd_unknown: bool = False  # a cd to a directory chosen by an expansion
    dirs_cut: bool = False  # more directories than DIRS_CAP: the ones past it were not kept


def _scope(parsed: Parsed, cwd: str | None, home: str) -> _Scope:
    scope = _Scope(env=assignments(parsed), dirs=[cwd] if cwd else [])
    for sub in _all_subs(parsed):
        for argv in _argvs(sub, scope.env):
            if argv[0] not in ("cd", "pushd"):
                continue
            args = [a for a in argv[1:] if a == "-" or not a.startswith("-")]
            target = args[0] if args else "~"
            if target == "-":
                continue  # the previous directory is among dirs already
            word = _expand_home(target, cwd, home)
            if UNRESOLVED.search(word):
                scope.cd_unknown = True
                continue
            for base in list(scope.dirs) or [""]:
                new = os.path.normpath(word if os.path.isabs(word) else os.path.join(base, word))
                new = "/" + new.lstrip("/") if new.startswith("//") else new
                if os.path.isabs(new) and new not in scope.dirs:
                    if len(scope.dirs) >= DIRS_CAP:
                        scope.dirs_cut = True
                        continue
                    scope.dirs.append(new)
    return scope


# ── rm and the other catastrophes: targets are resolved against the directory, not just compared ──

#: Directories that are the system or a whole home: wiping one, or everything in it, is never wanted.
_PROTECTED_DIRS = frozenset(t for t in _DANGEROUS_RM_TARGETS if t.startswith("/") and "*" not in t)
_TOP_LEVEL = tuple(sorted(d[1:] for d in _PROTECTED_DIRS if d != "/"))
#: A glob made only of wildcards and dots (``*``, ``.*``, ``.[!.]*``, ``?*``): it matches everything.
_BROAD_GLOB = re.compile(r"[*?.\[\]!^-]*[*?][*?.\[\]!^-]*")
_GLOBS = frozenset("*?[")
_WHOLE_DIR = frozenset({"*", ".", "..", ".*", "./*", "../*", "./", "../"})


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


def _expand_home(t: str, cwd: str | None, home: str) -> str:
    """``t`` with ``~``, ``~user``, ``$HOME`` and, when the directory is known, ``$PWD`` expanded."""
    if t.startswith("~") and t != "~" and not t.startswith("~/"):
        t = os.path.expanduser(t)
    return expand_vars(t, cwd, home)


def _resolved(t: str, cwd: str | None, home: str, dirs: list[str]) -> list[str]:
    """The absolute normalised paths ``t`` can name: itself when absolute, else joined to every directory the command
    may run in. A trailing glob component stays."""
    t = _expand_home(t, cwd, home)
    if not t:
        return []
    out: list[str] = []
    for base in [""] if os.path.isabs(t) else (dirs or ([cwd] if cwd else [])):
        p = os.path.normpath(os.path.join(base, t))
        p = "/" + p.lstrip("/") if p.startswith("//") else p
        if os.path.isabs(p) and p not in out:
            out.append(p)
    return out


def _protected_hit(p: str, home: str) -> str | None:
    """``rm-rf-root`` / ``rm-rf-home`` when the absolute path ``p`` is a protected directory or a glob that matches
    everything in one (``/usr/*``, ``~/.*``, ``/e*``)."""
    protected = _PROTECTED_DIRS | ({home} if home and home != "/" else set())
    parent, last = os.path.split(p)
    if _GLOBS & set(last):
        hit = (parent in protected and bool(_BROAD_GLOB.fullmatch(last))) or (
            parent == "/" and any(fnmatch.fnmatchcase(n, last) for n in _TOP_LEVEL)
        )
    else:
        hit = p in protected
    if not hit:
        return None
    return "rm-rf-home" if home in (p, parent) or "/home" in (p, parent) else "rm-rf-root"


def _rm_split(args: list[str]) -> tuple[bool, list[str]]:
    """(recursive, targets) of ``rm`` ``args``, whatever the flag spelling (-rf, -fr, -r -f, -R, --recursive, ...)."""
    flags: list[str] = []
    targets: list[str] = []
    done = False
    for t in args:
        if not done and t == "--":
            done = True
        elif not done and t.startswith("-") and t != "-":
            flags.append(t)
        else:
            targets.append(t)
    recursive = any(t == "--recursive" or (not t.startswith("--") and ("r" in t or "R" in t)) for t in flags)
    return recursive, targets


def _rm_check(argv: list[str], cwd: str | None, home: str, dirs: list[str] | None = None) -> str | None:
    """``rm`` with a recursive flag on /, ~, $HOME, a top-level system dir or everything in one, whatever the flag
    spelling or path spelling (``/.``, ``~/.``, ``/usr/..``, ``$PWD`` at /, ``/usr/*``, ``~/.*``, and relative ones:
    ``..`` or ``*`` in / or $HOME, also after a ``cd``)."""
    if not argv or argv[0] != "rm":
        return None
    recursive, targets = _rm_split(argv[1:])
    if not recursive:
        return None
    dangerous = _DANGEROUS_RM_TARGETS | {home, home + "/*"}
    for t in targets:
        target = _expand_target(t, cwd, home) if t else t
        if target in dangerous:
            homeish = "~" in t or "HOME" in t or t.startswith("/home") or target in (home, home + "/*")
            return "rm-rf-home" if homeish else "rm-rf-root"
        for p in _resolved(t, cwd, home, dirs if dirs is not None else []):
            hit = _protected_hit(p, home)
            if hit:
                return hit
    return None


_BLOCK_DEVICE = re.compile(r"/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|disk|rdisk)")
#: Commands whose operands (``cp``/``mv``/``install``: the last one) are written to.
_DEVICE_WRITERS = frozenset({"cp", "mv", "install", "tee", "shred", "wipefs", "blkdiscard", "truncate"})
_DEST_FIRST = frozenset({"cp", "mv", "install"})


def _is_block_device(word: str) -> bool:
    w = word.strip("'\"")
    if not w.startswith("/"):
        return False
    p = os.path.normpath(w)
    p = "/" + p.lstrip("/") if p.startswith("//") else p
    return _BLOCK_DEVICE.match(p) is not None


def _device_redirect_check(sub: str) -> str | None:
    """``echo x > /dev/sda``, ``cat img >| /dev/nvme0n1``, ``1<>/dev/vda``: a redirect onto a block device."""
    return "block-device-write" if any(_is_block_device(t) for t in WRITE_REDIRECT.findall(sub)) else None


def _device_check(argv: list[str]) -> str | None:
    """``cp img /dev/sda``, ``tee /dev/sdb``, ``wipefs -a /dev/sda`` ...: a block device as the thing written. Reading
    one (``dd if=/dev/sda``, ``cp /dev/sda disk.img``, ``fdisk -l``) is not."""
    if not argv or argv[0] not in _DEVICE_WRITERS:
        return None
    args = argv[1:]
    positional = [a for a in args if not a.startswith("-")]
    dests = positional[-1:] if argv[0] in _DEST_FIRST else positional
    dests += [b for a, b in zip(args, args[1:], strict=False) if a in ("-t", "--target-directory")]
    dests += [a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")]
    return "block-device-write" if any(_is_block_device(d) for d in dests) else None


def _recursive_root_check(argv: list[str], cwd: str | None, home: str, dirs: list[str]) -> str | None:
    """``chmod -R 777 /``, ``chown -R me /*``: a recursive permission change of the whole file system."""
    if not argv or argv[0] not in ("chmod", "chown", "chgrp"):
        return None
    args = argv[1:]
    if not any(a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "R" in a[1:]) for a in args):
        return None
    for a in args:
        if not a.startswith("-") and any(p in ("/", "/*") for p in _resolved(a, cwd, home, dirs)):
            return "recursive-chmod-root"
    return None


_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
#: find options that come before the start points.
_FIND_OPTS = frozenset({"-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"})


def _find_roots(args: list[str]) -> list[str]:
    """The start points of ``find`` ``args`` (``.`` when there are none)."""
    i = 0
    while i < len(args):
        if args[i] in _FIND_OPTS or (args[i].startswith("-O") and args[i][2:].isdigit()):
            i += 1
        elif args[i] == "-D":
            i += 2
        else:
            break
    roots: list[str] = []
    while i < len(args) and not args[i].startswith("-") and args[i] not in ("(", ")", "!", ","):
        roots.append(args[i])
        i += 1
    return roots or ["."]


def _find_payloads(args: list[str]) -> list[str]:
    """The command lines ``find`` runs with -exec/-execdir/-ok/-okdir (to the ``;`` or ``+``), once per start point
    with ``{}`` replaced by it: ``find / -exec rm -rf {} +`` runs ``rm -rf /``."""
    roots = _find_roots(args)
    out: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in _FIND_EXEC:
            j = i + 1
            while j < len(args) and args[j] not in (";", "+"):
                j += 1
            cmd = args[i + 1 : j]
            if cmd:
                out.extend(shlex.join(c.replace("{}", root) for c in cmd) for root in roots)
            i = j
        i += 1
    return out


#: ``find`` tests that pick some of the files under a root (``-type`` and ``-maxdepth`` only shape the walk). Not
#: ``-readable``, ``-writable``, ``-fstype`` or ``-context``: they hold for every file of a home directory.
_FIND_SELECTORS = frozenset(
    {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname"}
    | {"-newer", "-anewer", "-cnewer", "-mtime", "-mmin", "-atime", "-amin", "-ctime", "-cmin", "-used", "-size"}
    | {"-user", "-group", "-empty", "-perm", "-samefile", "-inum", "-uid", "-gid", "-nouser", "-nogroup", "-links"}
    | {"-xtype", "-executable"}
)
#: Arguments with which one of those holds for (nearly) every file: ``-xtype f`` is ``-type f``, every file has a link
#: (``-links +0``) and most have only one (``-links 1``, ``-links -2``), ``-used +0`` is any file read after it changed.
_FIND_UNIVERSAL = {
    "-xtype": re.compile(r".*[fd].*"),
    "-links": re.compile(r"-\d+|\+0+|0*1"),
    "-used": re.compile(r"\+0+"),
}
#: ``-newerXY``: a time of the file (a, B, c, m) against a time of the reference (the same, or t: a date string).
_FIND_NEWER = re.compile(r"-newer[aBcm][aBcmt]")
#: How many ( groups ) deep :func:`_find_scan` goes: a deeper one picks nothing (recursion on a long command).
_FIND_GROUP_DEPTH = 32


def _find_scan(args: list[str], _depth: int = 0) -> tuple[bool, bool]:
    """(the tests of a ``find`` expression (``args``) are true for only some of the files, every ``-delete`` in it is
    preceded by tests that are). Picking some means every ``-o`` branch holds a selector or a ( group ) that does, and
    not a negated one: ``! -name keep`` and ``-not -user me`` pick the complement, ``-name x -o -delete`` runs the
    action on whatever ``-name x`` does not match, and after a ``,`` only the tests that follow it count
    (``-name x , -delete`` deletes every file)."""
    branches = [False]
    guarded = True
    negated = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a in ("!", "-not"):
            negated = not negated
            continue
        if a in ("-o", "-or"):
            branches.append(False)
        elif a == ",":
            branches[:] = [False]
        elif a == "-delete":
            guarded = guarded and all(branches)
        elif a == "(":
            start, depth = i, 1
            while i < len(args) and depth:
                depth += (args[i] == "(") - (args[i] == ")")
                i += 1
            picks, ok = _find_scan(args[start : i - 1], _depth + 1) if _depth < _FIND_GROUP_DEPTH else (False, False)
            guarded = guarded and ok
            branches[-1] = branches[-1] or (not negated and picks)
        elif a in _FIND_SELECTORS or _FIND_NEWER.fullmatch(a):
            universal = a in _FIND_UNIVERSAL and _FIND_UNIVERSAL[a].fullmatch(args[i] if i < len(args) else "")
            branches[-1] = branches[-1] or (not negated and not universal)
        negated = False
    return all(branches), guarded


def _find_picks_some(args: list[str]) -> bool:
    """The tests of a ``find`` expression (``args``) are true for only some of the files, see :func:`_find_scan`."""
    return _find_scan(args)[0]


def _find_check(argv: list[str], cwd: str | None, home: str, dirs: list[str]) -> str | None:
    """``find / ... -delete``, and ``find ~ -delete`` / ``find ~ -type f -delete`` (the whole home directory or a system
    one like /etc, with no test that picks some of its files: ``find ~ -name '*.pyc' -delete`` is an ordinary
    clean-up; a negated test, one on the far side of ``-o`` or ``,`` or one after ``-delete`` picks nothing, and
    every ``-delete`` of the line counts, see :func:`_find_scan`)."""
    if not argv or argv[0] != "find" or "-delete" not in argv:
        return None
    picks_some = _find_scan(argv[1:])[1]
    for root in _find_roots(argv[1:]):
        resolved = _resolved(root, cwd, home, dirs)
        if any(p in ("/", "/*") for p in resolved):
            return "find-delete-root"
        if not picks_some:
            for p in resolved:
                if hit := _protected_hit(p, home):
                    return hit
    return None


# ── what a person has to decide ──


def capped(parsed: Parsed, cwd: str | None, home: str, scope: _Scope | None = None) -> str | None:
    """Why the checks saw only some spellings of the command line, else None: it gives a variable more values, has a
    brace expansion with more words, or cds to more directories than the caps keep. A deny that a dropped spelling
    would have hit (``a=x1; ... a=rm; $a -rf /``) did not fire, so nothing vouches for the line."""
    scope = scope or _scope(parsed, cwd, home)
    if scope.dirs_cut:
        return f"the line cds to more than {DIRS_CAP} directories"
    if any(_cut_short(sub, scope.env) for sub in _all_subs(parsed)):
        return (
            f"a variable has more than {COMBINATIONS_CAP} values or a brace expansion more than {BRACE_WORDS_CAP} words"
        )
    return None


def needs_human(parsed: Parsed, cwd: str | None, home: str) -> str | None:
    """Why the command line cannot be vouched for even though no hardline rule fired, else None: a shell reads a
    script that is not on the command line, the command itself or an ``rm`` argument is a shell expansion whose value
    is not known here (``$cmd -rf /``, ``rm -rf $(echo /)``), an ``rm -r`` of a whole directory runs after a ``cd`` to
    an unknown one, or a cap dropped spellings the checks never saw (:func:`capped`). The engine asks a person (and
    never auto-allows)."""
    if parsed.opaque_script:
        return parsed.opaque_script
    scope = _scope(parsed, cwd, home)
    if cut := capped(parsed, cwd, home, scope):
        return cut
    for sub in _all_subs(parsed, shell_only=True):
        for argv in _argvs(sub, scope.env):
            if UNRESOLVED.search(expand_vars(argv[0], cwd, home)):
                return "the command to run is a shell expansion"
            if argv[0] != "rm":
                continue
            if any(UNRESOLVED.search(expand_vars(a, cwd, home)) for a in argv[1:]):
                return "rm takes an argument that is a shell expansion"
            recursive, targets = _rm_split(argv[1:])
            if recursive and scope.cd_unknown and any(t in _WHOLE_DIR for t in targets):
                return "rm removes a whole directory after a cd to a directory chosen by an expansion"
    return None


def risky(parsed: Parsed) -> str | None:
    """A command line that is not a hardline deny but always goes to a person: a force push with no explicit
    refspec (it pushes to the upstream, which may be main; with a refspec the hardline already judged it)."""
    for sub in _all_subs(parsed):
        for argv in _argvs(sub, {}):
            if argv[:2] == ["git", "push"]:
                force, positional = _push_parts(argv[2:])
                if force and not positional[1:]:
                    return "A force push without an explicit branch goes to the upstream, which may be main"
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
    #: nesting past the parse limit: what is inside was not parsed (``unterminated`` is set too)
    too_deep: bool = False
    #: a shell reads a script from a pipe or file that is not on the command line: why it cannot be checked ("" if not)
    opaque_script: str = ""
    #: parsed from a quoted argument of an awk / sed / python / find ... command: looked at like shell code (a denied
    #: command cannot hide there) but it is most likely no shell code, so needs_human does not judge it
    script: bool = False
    #: the simple commands the line has when ``>|`` is read as a pipe (as it was before the audit); the engine rates
    #: these too, so the better reading never turns an ask of the old one into an allow
    legacy_subs: list[str] = field(default_factory=list)


_LINE_END = re.compile(r"[ \t]*(?:\n|$)")


def _match_paren(text: str, open_idx: int, arith: bool | None = None) -> int:
    """Index of the ``)`` that closes the ``(`` at ``open_idx`` (quote, escape, comment and heredoc aware); -1 when
    unbalanced. In arithmetic (``arith``; by default when the text there is ``((``) a ``<<`` is a shift and a ``#``
    starts no comment."""
    depth, i, n = 0, open_idx, len(text)
    heredocs: list[HeredocWord] = []
    if arith is None:
        arith = text.startswith("((", open_idx)  # $((...)): a << in it is a shift
    while i < n:
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
        if c == "#" and not arith and i > open_idx + 1 and text[i - 1] in " \t\n;&|()<>":
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


def _arith_end(text: str, i: int) -> int:
    """Index of the last ``)`` of the arithmetic command ``((...))`` at ``i``, where ``<<`` is a shift, or -1 when it
    is not one: as in bash, the inner ``(`` must close right before a ``)`` (``((a) )`` is two subshells)."""
    inner = _match_paren(text, i + 1, arith=True)
    return inner + 1 if inner > 0 and text.startswith(")", inner + 1) else -1


#: How many ``$(...)`` levels _scan follows before it stops looking inside (the call stack is finite).
_MAX_SCAN_NEST = 48


def _scan(text: str, _nest: int = 0) -> Parsed:
    """Quote-aware split into simple commands and pipelines, plus the payloads of the substitutions found on the way.
    ``#`` starts a comment only at the start of a word outside quotes; ``\\<newline>`` joins lines; ``<<`` starts a
    heredoc only outside quotes and outside an open ``((...))``, ``${...}``, ``$[...]``, ``[...]`` or ``a=(...)``,
    however far back it opened (its body is not commands; the substitutions in an unquoted body are payloads of the
    command it is fed to; a shell reading one runs the body: see _code_heredocs). In ``((...))`` a ``<<`` is a shift;
    one that bash may not read as a heredoc, or whose body end bash may see elsewhere (in the other brackets, an odd
    delimiter word), makes the command unterminated and the next lines stay commands."""
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
        if _nest >= _MAX_SCAN_NEST:  # not followed: the payload is never checked, so this needs a person
            out.unterminated = out.too_deep = True
            return end + 1, text[at : end + 1]
        inner = _scan(payload, _nest + 1)
        if inner.too_deep:
            out.unterminated = out.too_deep = True
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
        out.unterminated = out.unterminated or _scan(text[at + 1 : end], _nest + 1).open_heredoc
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
        elif text.startswith(">|", i):  # the noclobber override is a redirection, not a pipe
            emit(">|")
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
                elif top != "((" and text.startswith("((", i) and (end := _arith_end(text, i)) > 0:
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


def _ssh_remote_command(args: list[str]) -> str:
    """The remote command of ``ssh`` ``args``: everything after the host, joined (``ssh host rm -rf /``)."""
    return " ".join(_ssh_command(args))


#: Commands whose quoted arguments are shell code (the others in LAUNCHERS only take a script or a pattern there).
_SHELL_PAYLOAD_LAUNCHERS = _SHELLS | {"eval", "ssh", "su", "watch", "flock"}


def _launcher_payloads(sub: str) -> tuple[list[str], list[str]]:
    """The strings ``sub`` runs as commands: quoted arguments of ``bash -c``, ``ssh host "..."``, ``sudo sh -c ...``,
    the joined arguments of ``eval``, the values of ``ssh -oProxyCommand=...`` (see ssh_option_commands), the command
    ``ssh host cmd args`` runs, what ``find -exec`` runs (see _find_payloads) and ``flock -c CMD``; redirections in
    front (``>/dev/null bash -c ...``) do not hide any of it. The second list holds the quoted arguments of the other
    launchers (awk, sed, python, find, xargs ...): scripts and patterns, most likely not shell code."""
    shell: list[str] = []
    script: list[str] = []

    def quoted(args: list[str]) -> list[str]:
        return [t for t in args if not t.startswith("-") and re.search(r"\s|[;&|$`<>()]", t)]

    spellings = (sub, decode_ansi_c(sub)) if "$'" in sub else (sub,)
    for toks in [t for sp in spellings for t in (_tokens(sp), _tokens(strip_redirects(sp)))]:
        pre = _strip_wrappers(toks)
        if len(pre) > 1 and os.path.basename(pre[0]) == "eval":
            shell.append(" ".join(pre[1:]))
        if pre and os.path.basename(pre[0]) == "flock":
            for k, t in enumerate(pre):
                if t in ("-c", "--command") and k + 1 < len(pre):
                    shell.append(pre[k + 1])
                elif t.startswith("--command="):
                    shell.append(t.split("=", 1)[1])
        real = normalize_argv(toks)
        # as written too: `watch 'rm -rf /'` normalises to the one word `rm -rf /`, and the wrapper is what quotes it
        for argv in (pre, real):
            if argv and os.path.basename(argv[0]) in LAUNCHERS:
                into = shell if os.path.basename(argv[0]) in _SHELL_PAYLOAD_LAUNCHERS else script
                into += quoted(argv[1:])
        if real:
            shell += ssh_option_commands(real[0], real[1:])
            if real[0] == "find":
                shell += _find_payloads(real[1:])
            elif real[0] == "ssh":
                remote = _ssh_remote_command(real[1:])
                if remote:
                    shell.append(remote)
    shell = list(dict.fromkeys(shell))
    return shell, [t for t in dict.fromkeys(script) if t not in shell]


_HERESTRING = re.compile(r"<<<[ \t]*('[^']*'|\"(?:[^\"\\]|\\.)*\"|[^\s;&|<>()]+)")
#: ``< file`` (not ``<<``, ``<<<``, ``<&``, ``<(``, ``<>``): the shell's stdin is that file.
_STDIN_FILE = re.compile(r"(?:^|[^<>&\d])0?<(?![<&(>])[ \t]*([^\s;&|<>()]+)")


def _unescape(text: str) -> str:
    """``\\n``, ``\\x72`` ... as echo -e / printf / dash's echo read them."""
    with contextlib.suppress(UnicodeDecodeError, ValueError):
        return codecs.decode(text.encode("latin-1", "backslashreplace"), "unicode_escape")
    return text


def _literal_output(stage: str, env: dict[str, list[str]]) -> list[str] | None:
    """What a pipeline stage that is a plain ``echo`` / ``printf`` prints, as candidate texts (escapes decoded and not,
    format and arguments apart and joined); None when the stage is anything else."""
    texts: list[str] = []
    for variant in expand_assigned(stage, env):
        toks = _tokens(strip_redirects(variant))
        while toks and _is_assignment(toks[0]):
            toks = toks[1:]
        if not toks or toks[0] not in ("echo", "printf"):
            return None
        args = toks[1:]
        if toks[0] == "echo":
            while args and re.fullmatch(r"-[neE]+", args[0]):
                args = args[1:]
            texts.append(" ".join(args))
        else:
            texts += [args[0] if args else "", *args[1:], " ".join(args[1:])]
    decoded = [_unescape(t) for t in texts if "\\" in t]
    return [t for t in dict.fromkeys([*texts, *decoded]) if t.strip()]


#: Stages that print their stdin unchanged (a ``cat <<EOF`` in front of a shell hands it the body as written).
_PASSTHROUGH = {"cat": frozenset({"-"}), "tee": frozenset({"-", "-a", "-i", "/dev/null"})}


def _passthrough(stage: str) -> bool:
    argv = normalize_argv(_tokens(strip_redirects(stage)))
    return bool(argv) and argv[0] in _PASSTHROUGH and set(argv[1:]) <= _PASSTHROUGH[argv[0]]


def _stdin_scripts(parsed: Parsed) -> list[str]:
    """The scripts a shell is fed on stdin that can be read off the command line: a here-string, or what the previous
    stage of the pipeline ``echo``/``printf``s. (The body of a heredoc a shell runs is parsed as a script by
    :func:`_parse`, see Parsed.code_heredoc; so is one a ``cat <<EOF`` hands to it down the pipe.) Anything else
    (``cat x | sh``, ``base64 -d | bash``, ``sh < file``, ``base64 -d <<EOF | sh``) is a script that is not on the
    command line: ``parsed.opaque_script`` says so."""
    why = "a shell reads a script that is not on the command line (a pipe or a file feeds it)"
    env = assignments(parsed)
    scripts: list[str] = []
    feeder: dict[int, int] = {}  # sub index -> index of the previous stage of its pipeline
    base = 0  # the pipelines list the subs in order
    for stages in parsed.pipelines:
        feeder.update({base + j: base + j - 1 for j in range(1, len(stages))})
        base += len(stages)
    mapped = base == len(parsed.subs)
    for k, sub in enumerate(parsed.subs):
        argv = normalize_argv(_tokens(strip_redirects(sub)))
        if not argv or argv[0] not in _SHELLS or not _runs_stdin_as_code(argv):
            continue
        words = [" ".join(_tokens(m.group(1))) for m in _HERESTRING.finditer(sub)]
        scripts += words
        files = [f for f in _STDIN_FILE.findall(sub) if f != "/dev/null"]
        if files:
            parsed.opaque_script = why
        if parsed.heredocs[k] and parsed.code_heredoc[k] != "shell":
            scripts += [h.body for h in parsed.heredocs[k]]  # not marked as a program: still look at the body
        if parsed.heredocs[k] or words or files:
            continue  # a heredoc fed to this shell is its script, see above
        if not mapped:
            parsed.opaque_script = why  # the pipelines do not line up with the commands: the producer is unknown
            continue
        if k not in feeder:
            continue
        producer = feeder[k]
        texts = _literal_output(parsed.subs[producer], env)
        if texts is not None:
            scripts += texts
        elif not (parsed.heredocs[producer] and _passthrough(parsed.subs[producer])):
            parsed.opaque_script = why
    return scripts


def parse(command: str, _depth: int = 0) -> Parsed:
    """Split ``command`` into simple commands and collect every command nested inside it (recursively). Nesting past
    the limit is not followed: the result is marked ``unterminated`` (and ``too_deep``), so nothing vouches for it."""
    if _depth:
        return _parse(command, _depth)
    try:
        return _parse(command, 0)
    except RecursionError:  # the stack ran out before the limit did: nothing below was checked
        text = command.strip()
        return Parsed(
            subs=[text],
            unterminated=True,
            too_deep=True,
            pipelines=[[text]],
            clean=command,
            sub_payloads=[[]],
            heredocs=[[]],  # one entry per sub in each of these, as _scan keeps them
            code_heredoc=[""],
            remote_heredoc=[[]],
        )


#: How many levels of ``$(...)`` / ``bash -c`` / ... parse() follows.
_MAX_DEPTH = 6


def _parse(command: str, depth: int) -> Parsed:
    parsed = _scan(command)
    payloads = [(p, False) for ps in parsed.sub_payloads for p in ps]
    for sub in parsed.subs:
        shell, script = _launcher_payloads(sub)
        payloads += [(p, False) for p in shell] + [(p, True) for p in script]
    payloads += [(p, False) for p in _stdin_scripts(parsed)]
    if ">|" in command:  # before the audit `>|` split the line like a pipe: keep looking at that reading as well
        legacy = _scan(command.replace(">|", "> |"))
        parsed.legacy_subs = legacy.subs
        payloads.append((command.replace(">|", "> |"), True))
    # a heredoc a shell runs is its script (`bash <<'EOF'`, `cat <<EOF | sh`): checked like `bash -c "..."`, except for
    # its quotes (a stray one in a comment of a long script is not worth a prompt). The program of another
    # interpreter is not shell: such a heredoc is only kept out of the allowlist (see engine._decide_bash).
    scripts = [(k, h.body) for k, hs in enumerate(parsed.heredocs) if parsed.code_heredoc[k] == "shell" for h in hs]
    if (payloads or scripts) and depth >= _MAX_DEPTH:  # cut off: what is inside is not checked, so a person decides
        parsed.unterminated = parsed.too_deep = True
        return parsed
    for payload, is_script in payloads:
        inner = _parse(payload, depth + 1)
        inner.script = is_script
        parsed.children.append(inner)
        parsed.nested.extend([*inner.subs, *inner.nested])
        parsed.unterminated = parsed.unterminated or inner.unterminated
        parsed.comment_quote = parsed.comment_quote or inner.comment_quote
        parsed.too_deep = parsed.too_deep or inner.too_deep
        parsed.opaque_script = parsed.opaque_script or inner.opaque_script
    for k, body in scripts:
        inner = _parse(body, depth + 1)
        parsed.children.append(inner)
        parsed.nested.extend([*inner.subs, *inner.nested])
        if inner.too_deep:
            parsed.unterminated = parsed.too_deep = True
        parsed.opaque_script = parsed.opaque_script or inner.opaque_script
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
