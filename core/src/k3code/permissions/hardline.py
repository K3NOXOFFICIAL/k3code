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
from functools import lru_cache

#: (name, regex) pairs matched against the raw command line and every simple command.
HARDLINE_PATTERNS: list[tuple[str, str]] = [
    # rm -rf / and ~ are caught by _rm_check (token based: any flag spelling, quotes, sudo/env wrappers)
    # env / printenv dumping the environment are caught by _env_dump_check (token based: it lets `env VAR=x cmd`,
    # `env -u X cmd` and `env -i cmd` fall through to the real command's checks, which a raw regex could not)
    ("mkfs", r"\bmkfs(?:\.\w+)?\b"),  # mkfs / mkfs.ext4 ...
    ("dd-to-device", r"\bdd\b.*\bof=/dev/"),  # dd of=/dev/...
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
        r"(?=[^\n;|&]*(?:[\s:+]|refs/heads/)(?:main|master)(?![^\s;|&]))",
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
    long command line a second time."""
    home = home if home is not None else os.path.expanduser("~")
    budget = [_SCAN_BUDGET]
    parsed = parse(command, budget=budget)
    # past the scan budget a substitution's end is unresolved and the text after it was never read as commands, and
    # past the nesting the parser follows the commands in there were never read either: whatever runs there is
    # unknown, so the command is denied rather than allowed unchecked (yolo too). The checks below cannot lift that
    # deny and would tokenise every nesting level of such a command, so they are skipped.
    if budget[0] < 0 or parsed.too_deep:
        return ("scan-budget" if budget[0] < 0 else "scan-depth"), parsed
    return _match(parsed.clean, extra_patterns) or _check_parsed(parsed, extra_patterns, cwd, home), parsed


def _check_parsed(parsed: Parsed, extra: list[str] | None, cwd: str | None, home: str) -> str | None:
    for k, sub in enumerate(parsed.subs):
        argv = normalize_argv(_tokens(sub))
        hit = (
            _match(sub, extra)
            or _match(" ".join(argv), extra)
            or _simple_check(sub)
            or _rm_check(argv, cwd, home)
            or _git_push_check(argv)
            or _env_dump_check(sub)
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


def _match(text: str, extra_patterns: list[str] | None) -> str | None:
    for name, rx in _COMPILED:
        if rx.search(text):
            return name
    for i, rx in enumerate(extra_patterns or []):
        if not rx.strip():
            continue  # a blank pattern would match every command (config load and doctor name it)
        if re.search(rx, text):
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
    spec: tuple[str, frozenset[str], int],
    reads: list[str] | None = None,
    chdirs: list[str] | None = None,
    replaces: list[str] | None = None,
) -> list[str]:
    """``args`` after the wrapper's own options (and its ``positionals``): the wrapped command, empty when there is
    none. ``env -S STRING`` / ``--split-string=STRING`` is read the way GNU env reads it: the words of STRING take the
    option's place and are parsed in turn as env's own options, assignments and command, so ``env -S '-u PATH'`` is
    ``env -u PATH`` (nothing to run: it prints the environment) and ``env -S 'X=1 make' test`` runs ``make test``."""
    short_val, long_val, positionals = spec
    read_short, read_long = _WRAPPER_READ_OPTS.get(name, ("", frozenset()))
    chdir_short, chdir_long = _WRAPPER_CHDIR_OPTS.get(name, ("", frozenset()))
    args = list(args)
    i = 0
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
            if name == "xargs" and replaces is not None and len(key) >= 5 and "--replace".startswith(key):
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
        args[i:i] = split  # STRING's words are never longer than STRING, so this ends
    return args[i + positionals :]


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
    # every round drops at least a wrapper name, or turns an env -S string into its (shorter) words
    for _ in range(sum(len(t) + 1 for t in tokens) + 1):
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
                if argv[i] == "-C" and chdirs is not None and i + 1 < len(argv):
                    chdirs.append(argv[i + 1])
                i += 2 if argv[i] in _GIT_VALUE_OPTS else 1
            return [name, *argv[i:]]
        spec = _WRAPPER_SPECS.get(name)
        if spec is None:
            return argv
        rest = _skip_wrapper_options(name, argv[1:], spec, reads, chdirs, replaces)
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
    if delete and any(p.lstrip("+") in _MAIN_REFS for p in positional):
        return "git-push-delete-main"
    for refspec in positional[1:]:
        src, colon, dst = refspec.lstrip("+").rpartition(":")
        if dst in _MAIN_REFS and colon and not src:
            return "git-push-delete-main"
        if dst in _MAIN_REFS and (force or refspec.startswith("+")):
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
    argv = normalize_argv(_without_redirects(_tokens(sub)))
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
    from .engine import windows_drives  # engine imports this module

    drives = windows_drives()
    for t in targets:
        target = _expand_target(t, cwd, home) if t else t
        if target in dangerous:
            homeish = "~" in t or "HOME" in t or t.startswith("/home") or target in (home, home + "/*")
            return "rm-rf-home" if homeish else "rm-rf-root"
        if target and windows_rm_target(target.lower(), drives):
            return "rm-rf-root"  # a Windows drive, system folder or profile reached through WSL (issue #169)
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
    """Quoted arguments of ``bash -c``, ``ssh host "..."``, ``sudo sh -c ...``, the joined arguments of ``eval``, the
    values of ``ssh -oProxyCommand=...`` (see ssh_option_commands) and the command line of ``wsl.exe``, ``cmd.exe /c``
    and ``powershell.exe -Command`` (see _windows_command_line): strings run as commands, unquoted as the shell
    unquotes them (see _shell_words). An unquoted ``${...}`` is read both as written and as the text its word gives
    (``bash -c ${x:+'`a`'}`` runs `` `a` ``), a ``${...}`` in double quotes both as written and with the backslashes
    the shell drops there removed (``bash -c "${x:+\\`a\\`}"`` runs `` `a` ``), every combination of the two, and
    shlex's reading is checked as well. Identical readings are one reading."""
    out: list[str] = []
    # the readings differ only in a ${...} unit: without one they are all the first, read once (a line of thousands of
    # simple commands is read once per command, see engine._secret_access)
    flags = [(u, e) for u in (False, True) for e in (True, False)] if "${" in sub else [(False, True)]
    readings = [tuple(_shell_words(sub, unquote, unescape)) for unquote, unescape in flags]
    for toks in dict.fromkeys([*readings, tuple(_tokens(sub))]):  # shlex's reading too, as before _shell_words
        pre = _strip_wrappers(list(toks))
        if len(pre) > 1 and os.path.basename(pre[0]) == "eval":
            out.append(" ".join(pre[1:]))
        real = normalize_argv(list(toks))
        if real and _is_launcher_name(real[0]):
            out += [t for t in real[1:] if not t.startswith("-") and re.search(r"\s|[;&|$`<>()]", t)]
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
    payloads = [p for ps in parsed.sub_payloads for p in ps]
    for sub in parsed.subs:
        payloads.extend(_launcher_payloads(sub))
    if _depth >= _PARSE_MAX_DEPTH:
        # pathological nesting is cut off: commands nested in here are never read, so the command is too deep
        parsed.too_deep = bool(payloads) or "shell" in parsed.code_heredoc
        return parsed
    # a unit is read in more than one way (see _scan, _body_payloads, _launcher_payloads), and each reading is a subtree
    # of its own, so the work would multiply with every level of nesting. An identical reading is parsed once, and the
    # characters of every reading but the first are charged to the shared budget: past it the command is denied
    # (``scan-budget``) instead of read twice over
    for n, payload in enumerate(dict.fromkeys(payloads)):
        if n:
            budget[0] -= len(payload) or 1
        if budget[0] < 0:
            return parsed
        inner = parse(payload, _depth + 1, budget)
        parsed.children.append(inner)
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
            parsed.nested.extend([*inner.subs, *inner.nested])
            parsed.too_deep = parsed.too_deep or inner.too_deep
            # `ssh host <<EOF` runs the body on host: each command is also checked as `ssh host '<command>'`, the
            # spelling the rules about remote hosts match
            remote = [*inner.subs, *inner.nested]
            parsed.children += [
                _scan(f"{ssh} {shlex.quote(c)}", budget) for ssh in parsed.remote_heredoc[k] for c in remote
            ]
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
