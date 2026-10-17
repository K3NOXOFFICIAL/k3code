"""Modes plus the central decide() entry point.

Modes: ``default`` (ask edits + non-allowlisted bash), ``accept-edits``
(edits allowed, bash asks), ``plan`` (read-only + ``exit_plan``), ``auto``
(everything not denied allowed, side effects logged), ``yolo`` (everything
except hardline and the edits of protected files, see :func:`protected_path`, which always ask, as do the reads of the
credential stores and dot folders of ``$HOME`` that a root covers, see :func:`_decide_path`).
"""

from __future__ import annotations

import fnmatch
import functools
import os
import re
import shlex
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from k3code import paths

from . import hardline
from .rules import Action, Rule, evaluate, merge


class PermissionMode(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept-edits"
    PLAN = "plan"
    AUTO = "auto"
    YOLO = "yolo"
    # Legacy M0 aliases (kept so old configs keep working).
    ASK = "default"
    AUTO_EDIT = "accept-edits"

    @classmethod
    def _missing_(cls, value: object) -> PermissionMode | None:
        if not isinstance(value, str):
            return None
        legacy = {"ask": "default", "auto-edit": "accept-edits", "auto_edit": "accept-edits"}
        normalized = value.strip().lower()
        target = legacy.get(normalized) or normalized.replace("_", "-")
        for member in cls:
            if member.value == target:
                return member
        return None


class InvalidPermissionMode(ValueError):
    """A configured permission mode that is not a known name (a usage error for the CLI, a reply for the gateway)."""


def permission_mode_from_config(key: str, value: str) -> PermissionMode:
    """The PermissionMode for a config string; an unknown value raises InvalidPermissionMode naming the key."""
    try:
        return PermissionMode(value)
    except ValueError:
        raise InvalidPermissionMode(
            f"{key} {value!r} is not one of: default, accept-edits, plan, auto, yolo (or ask, auto-edit) "
            f"(set in config.yaml or K3CODE_{key.upper()})"
        ) from None


#: Builtin read-only bash allowlist: safe to run without asking, in every mode but plan. It covers the project only
#: (see _voids_allow: an argument outside the roots, a redirect or a substitution asks), and the flags of these commands
#: that are not known to be safe ask (_READONLY_OPTS, checked on the argv by _readonly_argv_ok; BUILTIN_BASH_ASK names
#: the best-known ones as patterns).
#: ``env``/``printenv`` are not here: they print the secrets in the environment.
BUILTIN_BASH_ALLOW = [
    "ls *",
    "cat *",
    "head *",
    "tail *",
    "wc *",
    "grep *",
    "rg *",
    "find *",
    "tree *",
    "file *",
    "stat *",
    "du *",
    "df *",
    "pwd *",
    "which *",
    "type *",
    "command -v *",
    "command -V *",
    "whoami",
    "id *",
    "uname *",
    "date *",
    "sort *",
    "uniq *",
    "cut *",
    "tr *",
    "jq *",
    "yq *",
    "diff *",
    "cmp *",
    "sha256sum *",
    "sha1sum *",
    "sha512sum *",
    "md5sum *",
    "basename *",
    "dirname *",
    "realpath *",
    "readlink *",
    "git status *",
    "git diff *",
    "git log *",
    "git show *",
    "git branch *",
    "git tag *",
    "git remote",
    "git remote -v",
    "git remote --verbose",
    "git remote get-url *",
    "git rev-parse *",
    "git blame *",
    "git ls-files *",
    "git describe *",
    "python --version",
    "python3 --version",
    "node --version",
    "go version",
    "cargo --version",
]

#: ``rg --pre CMD`` runs CMD on every file; ``git diff|log --output=FILE`` writes FILE; ``--ext-diff``/``--textconv``
#: run configured external programs; ``find -exec``/``-delete``/``-fprint``, ``sort -o``, ``tree -o``, ``file -C``
#: run or write; ``date -s`` sets the clock; ``git branch``/``git tag`` with these flags change refs. Longer patterns
#: win, so these ask over the allows above (and over a shorter user allow). _readonly_argv_ok also catches the
#: spellings a pattern cannot see (``sort -uo f``, ``"-exec"``, ``--out=f``, ``git branch new``, ``jq -n env``).
BUILTIN_BASH_ASK = [
    "rg* --pre*",
    "rg* --hostname-bin*",
    "git diff* --output*",
    "git log* --output*",
    "git show* --output*",
    "git diff* --ext-diff*",
    "git diff* --textconv*",
    "git log* --ext-diff*",
    "git log* --textconv*",
    "git show* --ext-diff*",
    "git show* --textconv*",
    "git blame* --textconv*",
    "find* -exec*",
    "find* -ok*",
    "find* -delete*",
    "find* -fprint*",
    "find* -fls*",
    "sort* -o*",
    "sort* --output*",
    "sort* --compress-program*",
    "tree* -o*",
    "file* -C*",
    "file* --compile*",
    "date* -s*",
    "date* --set*",
    "diff* --output*",
    "jq* -f*",
    "jq* --from-file*",
    "jq* -L*",
    "yq* -i*",
    "yq* --inplace*",
    "yq* --in-place*",
    "yq* -s*",
    "yq* --split-exp*",
    *(f"git branch* {f}*" for f in ("-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "-t")),
    *(f"git branch* {f}*" for f in ("--delete", "--move", "--copy", "--force", "--set-upstream", "--unset-upstream")),
    *(f"git branch* {f}*" for f in ("--edit-description", "--track", "--create-reflog")),
    *(f"git tag* {f}*" for f in ("-d", "-a", "-s", "-f", "-m", "-F", "-u", "-e")),
    *(f"git tag* {f}*" for f in ("--delete", "--annotate", "--sign", "--force", "--message", "--file", "--edit")),
    "git tag* --local-user*",
]

#: The builtin read-only programs a session started in ``$HOME`` runs without a person (see :func:`home_read_only`):
#: they print only what is named on the command line (a named credential file is refused first). Not the tree walkers
#: ``rg``/``tree``/``du`` (``rg`` prints every match below ``$HOME``), and not ``jq``/``yq`` (a program). ``find`` is
#: here, but only without the primaries that run a program, delete or write a file (see :data:`_HOME_FIND_RISKY`): on
#: its own it lists names, which the sandbox credential masking already covers.
_HOME_READERS = frozenset(
    {"ls", "cat", "head", "tail", "wc", "grep", "file", "stat", "df", "pwd", "which", "type", "command", "whoami"}
    | {"id", "uname", "date", "sort", "uniq", "cut", "tr", "diff", "cmp", "basename", "dirname", "realpath", "find"}
    | {"readlink", "sha256sum", "sha1sum", "sha512sum", "md5sum", "git"}
)
#: Characters that only the shell may act on, forbidden *outside* quotes in a home-reader command: a glob/brace
#: (expands to paths, possibly a hidden ``.env``), ``~`` (home expansion), ``$``/backtick (variable or substitution),
#: ``\`` (escape), a redirect or a comment. Inside quotes these are literal and reach the program unchanged.
_HOME_SHELL_CHARS = frozenset("*?[]{}~$`\\<>#")
#: A path component that starts with a dot (``.config``, ``..x``), not the ``.`` and ``..`` steps.
_DOT_SEGMENT = re.compile(r"(?:^|/)\.(?!\.?(?:/|$))")
#: The options that make a reader walk or dump a directory tree: (short letters, long options; GNU getopt takes any
#: unambiguous prefix of a long one). ``git diff --no-index DIR other`` dumps two trees.
_HOME_RECURSIVE: dict[str, tuple[str, tuple[str, ...]]] = {
    "ls": ("R", ("recursive",)),
    "grep": ("rRd", ("recursive", "dereference-recursive", "directories")),
    "diff": ("r", ("recursive",)),
    "git": ("", ("no-index",)),
}
#: ``find`` primaries that run a program, delete or write a file: with any of these ``find`` is not read-only. (The
#: builtin allowlist already asks for them, as for every primary :data:`_READONLY_OPTS` does not list; this is the
#: second check of the home sandbox.)
_HOME_FIND_RISKY = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
#: git subcommands that print file contents (a $HOME repo tracking dotenv or .bashrc would show them unprompted), and
#: the ``git log`` options that turn it into a patch dump.
_HOME_GIT_CONTENT = frozenset({"show", "diff", "blame"})
_HOME_GIT_PATCH_SHORT = "puc"
_HOME_GIT_PATCH_LONG = ("patch", "cc", "remerge-diff", "diff", "textconv", "ext-diff")


def _home_split(sub: str) -> list[str] | None:
    """Split a simple command into its words for :func:`home_read_only`, quote-aware. Single-quoted spans are literal;
    double-quoted spans are literal too, but only when they hold no ``$``, backtick or backslash (those would expand or
    escape). A ``$``, backtick, glob, brace, ``~``, backslash, redirect or comment *outside* quotes is something only
    the shell acts on, so the words we would check are not the words that run: the command is rejected (None). Returns
    the words with their quotes removed (the literal text that reaches the program), so a quoted glob such as
    ``'*.py'`` is a plain argument, never a path expansion."""
    words: list[str] = []
    cur: list[str] = []
    in_word = False
    i, n = 0, len(sub)
    while i < n:
        c = sub[i]
        if c in " \t":
            if in_word:
                words.append("".join(cur))
                cur, in_word = [], False
            i += 1
            continue
        in_word = True
        if c in ";|&":  # a separator should have been split by parse(); treat it as unparseable here
            return None
        if c == "'":
            j = sub.find("'", i + 1)
            if j < 0:
                return None  # unterminated
            cur.append(sub[i + 1 : j])
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and sub[j] != '"':
                j += 1
            if j >= n:
                return None  # unterminated
            inner = sub[i + 1 : j]
            if any(ch in inner for ch in "$`\\"):
                return None  # a variable, substitution or escape: the running word is not what is written
            cur.append(inner)
            i = j + 1
            continue
        if c in _HOME_SHELL_CHARS:
            return None  # a glob, ~, $, backtick, escape, redirect or comment outside quotes
        cur.append(c)
        i += 1
    if in_word:
        words.append("".join(cur))
    return words


def home_read_only(command: str) -> bool:
    """``command`` may run without a person in the read-only ``$HOME`` sandbox of a session started in the home
    directory: every part is a plain, non-recursive reader of :data:`_HOME_READERS`. Dotenv files lie anywhere below
    ``$HOME`` and the sandbox cannot hide them all, so nothing that walks a tree (``grep -r``, ``rg``, ``ls -R``,
    ``diff -r``, ``find -exec/-delete/...``) or expands a glob (``cat */.env``) qualifies; an explicitly named
    credential file is refused by :func:`decide` first. Credential stores sit in dot folders no list names in full
    (``~/.config/git/credentials``, ``~/.cache/huggingface/token``), so no argument may have a dot-segment, nor an
    option a dot at all (``--file=.x``, ``-f.x``). Quoted literal arguments are allowed (a single-quoted span never
    expands; a double-quoted span with no ``$``/backtick/backslash is literal too), so ``grep -n 'foo bar' notes`` and
    ``find . -name '*.py'`` run without a prompt -- the quoted glob is passed to the program, not expanded by the
    shell. An unquoted glob or ``$`` still asks. Over-matching only costs a prompt."""
    parsed = hardline.parse(command)
    if not parsed.subs or parsed.nested or parsed.children or parsed.unterminated:
        return False
    for sub in parsed.subs:
        words = _home_split(sub)
        if not words or words[0] not in _HOME_READERS:
            return False
        letters, longs = _HOME_RECURSIVE.get(words[0], ("", ()))
        if words[0] == "find" and any(w in _HOME_FIND_RISKY for w in words[1:]):
            return False
        if words[0] == "git":
            if any(word in _HOME_GIT_CONTENT for word in words[1:]):
                return False
            if "log" in words[1:] and any(
                (
                    word.startswith("--")
                    and any(full.startswith(word[2:].split("=", 1)[0]) for full in _HOME_GIT_PATCH_LONG)
                )
                or (
                    word.startswith("-")
                    and not word.startswith("--")
                    and any(c in word[1:] for c in _HOME_GIT_PATCH_SHORT)
                )
                for word in words[1:]
            ):
                return False
        for word in words[1:]:
            if ("." in word) if word.startswith("-") else bool(_DOT_SEGMENT.search(word)):
                return False
            if word.startswith("--"):
                name = word[2:].split("=", 1)[0]
                if any(full.startswith(name) for full in longs):
                    return False
            elif word.startswith("-") and any(ch in word[1:] for ch in letters):
                return False
    return True


#: Test, build and lint commands that run without asking in accept-edits and auto mode, and only there: they run the
#: project's code, so the decision carries ``sandbox_only`` and the agent loop runs them in the bwrap sandbox, or asks
#: when the sandbox cannot contain them (no usable bwrap, a root it refuses such as ``$HOME``). Like the read-only
#: allowlist they cover the project only (an argument outside the roots, a redirect or a substitution asks).
BUILTIN_SANDBOXED_ALLOW = [
    "pytest *",
    "python -m pytest *",
    "python3 -m pytest *",
    "uv run pytest *",
    "uv run ruff *",
    "uv run mypy *",
    *(f"{pm} test *" for pm in ("npm", "pnpm", "yarn")),
    *(f"{pm} run {s} *" for pm in ("npm", "pnpm", "yarn") for s in ("test", "lint", "build")),
    *(f"{pm} {s} *" for pm in ("pnpm", "yarn") for s in ("lint", "build")),
    *(f"npx {t} *" for t in ("tsc", "eslint", "vitest", "jest")),
    *(f"go {s} *" for s in ("test", "build", "vet")),
    *(f"cargo {s} *" for s in ("test", "build", "check", "clippy")),
    *(f"make {s} *" for s in ("test", "check", "lint")),
    "ruff *",
    "mypy *",
    "eslint *",
    "tsc *",
]
#: The modes BUILTIN_SANDBOXED_ALLOW applies in.
SANDBOXED_ALLOW_MODES = frozenset({"accept-edits", "auto"})
_SANDBOXED = frozenset(BUILTIN_SANDBOXED_ALLOW)

#: bash_output / bash_kill only reach background jobs of the calling session (the job registry enforces it); starting
#: one is ``bash`` with ``background: true`` and goes through the bash decision like any command.
PURE_TOOLS = frozenset(
    {
        "read",
        "grep",
        "glob",
        "todo",
        "skill",
        "mcp_tool_search",
        "task",
        "task_result",
        "bash_output",
        "bash_kill",
        "advisor",
    }
)
EDIT_TOOLS = frozenset({"write", "edit"})
READ_TOOLS = frozenset({"read", "grep", "glob"})  # path-taking read-only tools
EXIT_PLAN_TOOL = "exit_plan"
PLAN_MSG = "Plan mode is read-only; present the plan with exit_plan."


@dataclass
class Decision:
    """Outcome of decide(): what to do + what to show the user."""

    action: Action  # allow | ask | deny
    message: str | None = None  # denial reason (deny) or prompt hint (ask)
    patterns: list[str] = field(default_factory=list)  # evaluated patterns
    rule: Rule | None = None  # winning rule (or synthetic default)
    hardline: str | None = None  # hardline rule name, if denied by hardline
    auto_allowed: bool = False  # mode auto-allowed a would-be ask (log me)
    #: an ask that auto mode must not convert: a human answers it, an unattended run treats it as a deny
    needs_human: bool = False
    #: an ask that no mode and no rule converts, yolo included (see protected_path); headless it is a deny
    always_ask: bool = False
    #: an allow that holds only inside the sandbox (BUILTIN_SANDBOXED_ALLOW): the caller runs the command sandboxed,
    #: or asks when the sandbox cannot contain it
    sandbox_only: bool = False


@functools.lru_cache(maxsize=8)
def _builtin_defaults_cached(mode_value: str | None) -> tuple[Rule, ...]:
    """Builtin ruleset built once per mode: decide() rebuilt ~100 Rule objects on every call."""
    rules: list[Rule] = [Rule(tool=t, pattern="*", action="allow") for t in sorted(PURE_TOOLS - READ_TOOLS)]
    rules += [Rule(tool="bash", pattern=p, action="allow") for p in BUILTIN_BASH_ALLOW]
    if mode_value is not None and mode_value in SANDBOXED_ALLOW_MODES:
        rules += [Rule(tool="bash", pattern=p, action="allow") for p in BUILTIN_SANDBOXED_ALLOW]
    # Flags that make an allowed read-only command execute or write something: longer patterns win, so these ask.
    rules += [Rule(tool="bash", pattern=p, action="ask") for p in BUILTIN_BASH_ASK]
    rules.append(Rule(tool=EXIT_PLAN_TOOL, pattern="*", action="allow"))
    return tuple(rules)


def builtin_defaults(mode: PermissionMode | str | None = None) -> list[Rule]:
    """Builtin ruleset: pure tools + safe bash allow, writes/other bash ask. In accept-edits and auto mode the test,
    build and lint commands of BUILTIN_SANDBOXED_ALLOW are allowed too (sandboxed, see Decision.sandbox_only)."""
    mode_value = None if mode is None else PermissionMode(mode).value
    return list(_builtin_defaults_cached(mode_value))


_RANK: dict[str, int] = {"allow": 1, "ask": 2, "deny": 3}


def _norm(path: str | Path) -> str:
    return os.path.normpath(os.path.expanduser(str(path)))


def _real(path: str) -> str:
    """``os.path.realpath(path)``, but a NUL byte (or other OS-rejected path) in attacker-controlled input must
    reach a decision, not a crash: fall back to the normalized path so callers still get a comparable string.
    Containment checks (:func:`_inside`) must not treat this fallback as a resolved path; use :func:`_real_strict`
    there instead."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.normpath(path)


def _real_strict(path: str) -> str | None:
    """Like :func:`_real`, but a path the OS refuses (a NUL byte, say) resolves to ``None`` instead of a lexical
    guess: a containment check that cannot resolve a path must fail closed (not inside), never fail open by
    comparing an un-resolved string that happens to share a prefix with a root."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _abs(raw: str, cwd: str) -> str:
    p = os.path.expanduser(raw or ".")
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))


def _inside(path: str, roots: list[str]) -> bool:
    """``path`` lies under one of ``roots``, after resolving symlinks on both sides: a lexical check let
    ``proj/docs -> ../outside`` turn a write to ``docs/x`` into a write outside the project. A path that does not
    exist yet resolves through its nearest existing parent (realpath keeps the missing remainder as is). A path (or
    root) the OS refuses to resolve is never "inside": see :func:`_real_strict`."""
    real = _real_strict(path)
    if real is None:
        return False
    real_roots = [r for r in (_real_strict(root) for root in roots) if r is not None]
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in real_roots)


#: Files the edit tools never change without a person, in any mode and whatever the rules say: git runs them (hooks, a
#: ``filter`` or ``diff`` driver or ``core.fsmonitor`` in a git config, ``.gitattributes`` choosing the driver, the
#: ``.git`` FILE of a linked worktree naming its git directory), direnv runs ``.envrc`` on the next ``cd``, husky its
#: hooks on the next commit, and ``.k3code/config*`` sets permission rules and hooks. ``write`` and ``edit`` run in the
#: daemon, outside the sandbox, and the harness's own git runs there too (scope classification, worktrees).
#: Matched at any depth and case-insensitively; ``.git`` stands for everything under it. The state directory
#: ``$K3CODE_HOME`` is protected whole, under whatever name, see :func:`k3code_state`.
_PROTECTED = re.compile(
    r"(?:^|/)(?:\.git|\.gitattributes|\.gitmodules|\.envrc|\.husky|\.k3code/config[^/]*)(?:/|$)", re.IGNORECASE
)
#: The same in the home directory: shell startup files, user git config, fish config (``$HOME`` secret dirs such as
#: ``~/.ssh`` come from _secret_dirs).
_HOME_PROTECTED = (
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".bash_logout",
    ".profile",
    ".zshrc",
    ".zshenv",
    ".zprofile",
    ".zlogin",
    ".zlogout",
    ".kshrc",
    ".cshrc",
    ".tcshrc",
    ".login",
    ".gitconfig",
    ".config/git",
    ".config/fish",
)


def k3code_state(path: str) -> bool:
    """``path`` (absolute) is ``$K3CODE_HOME`` (resolved, whatever its name) or lies inside it: the trust store, the
    memory files, user skills, output styles and overlays that the next session loads as its own instructions.
    Compared on the path as written and on its realpath against the directory as set and as resolved, case-insensitively
    as the other guarded paths are. k3code's own writers do not go through ``decide``."""
    state = paths.home()
    guarded = [os.path.normpath(state), _real(state)]
    guarded = [g.casefold().rstrip(os.sep) for g in dict.fromkeys(guarded)]
    for p in dict.fromkeys((os.path.normpath(path), _real(path))):
        low = p.casefold()
        if any(low == g or low.startswith(g + os.sep) for g in guarded):
            return True
    return False


def _install_roots() -> list[str]:
    """What the next start of k3code runs, as paths: the install root (``K3CODE_DATA``, and the one the running
    process's own virtualenv sits in: ``<data>/current/venv`` or ``<data>/versions/<ver>/venv``, so a process that
    lacks the setting the install was made with still finds it); the ``k3code``, ``k3``, ``uv`` and ``uvx`` links and
    programs in ``<prefix>/bin`` (``<data>`` is ``<prefix>/share/k3code``; also ``~/.local/bin`` and ``K3_BIN_DIR``),
    of which the updater runs ``uv`` without the sandbox; and what shapes the systemd user unit and the environment
    it starts in. systemd applies a drop-in from any of its unit directories and reads ``user.conf``, ``environment.d``
    and the ``*.wants`` links next to the units, so the whole ``systemd`` and ``environment.d`` folders are guarded, in
    the default place and under ``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME`` (the daemon and the user manager may not agree
    on those), and ``$XDG_RUNTIME_DIR/systemd``."""
    datas = [paths.data_dir()]
    for venv in dict.fromkeys((Path(sys.prefix), Path(os.path.realpath(sys.prefix)))):
        if venv.name != "venv":
            continue  # a dev checkout's own environment is a project file, not an install
        if venv.parent.name == "current":
            datas.append(venv.parent.parent)
        elif venv.parent.parent.name == "versions":
            datas.append(venv.parent.parent.parent)
    bins = [Path.home() / ".local" / "bin"]
    bins += [d.parent.parent / "bin" for d in datas if d.parent.name == "share"]
    if explicit := os.environ.get("K3_BIN_DIR"):
        bins.append(Path(explicit).expanduser())
    config = [Path.home() / ".config", *_xdg("XDG_CONFIG_HOME")]
    share = [Path.home() / ".local" / "share", *_xdg("XDG_DATA_HOME")]
    out = [*datas, *(b / n for b in bins for n in ("k3code", "k3", "uv", "uvx"))]
    out += [c / n for c in config for n in ("systemd", "environment.d")]
    out += [d / "systemd" for d in (*share, *_xdg("XDG_RUNTIME_DIR"))]
    return [str(x) for x in out]


def _xdg(name: str) -> list[Path]:
    value = os.environ.get(name)
    return [Path(value).expanduser()] if value else []


def k3code_install(path: str) -> bool:
    """``path`` (absolute) is k3code's own installed code, one of its launcher links or programs, or what shapes the
    systemd user unit (see :func:`_install_roots`): what the next start runs, with no sandbox and no permission
    engine. Other files in ``~/.local/bin`` are not covered. Compared as written and resolved, against the locations as
    set and as resolved, case-insensitively."""
    guarded = [g for root in _install_roots() for g in (os.path.normpath(root), _real(root))]
    guarded = [g.casefold().rstrip(os.sep) for g in dict.fromkeys(guarded)]
    for p in dict.fromkeys((os.path.normpath(path), _real(path))):
        low = p.casefold()
        if any(low == g or low.startswith(g + os.sep) for g in guarded):
            return True
    return False


def protected_path(path: str) -> bool:
    """``path`` (absolute) is a file the edit tools never change without a person (see _PROTECTED), a shell startup
    file or git config in the home directory, under a credential directory (``~/.ssh``, ``~/.config/k3code``),
    under k3code's own state directory (see :func:`k3code_state`) or k3code's own install (see :func:`k3code_install`).
    Checked on the path as written and on its realpath, so a symlink does not hide the target."""
    if k3code_state(path):
        return True
    if k3code_install(path):
        return True
    home = os.path.expanduser("~")
    homes = (os.path.normpath(home), _real(home))
    guarded = [os.path.join(h, rel) for h in homes for rel in _HOME_PROTECTED]
    secrets = _secret_dirs()
    guarded += [*map(os.path.normpath, secrets), *map(_real, secrets)]
    guarded = [g.casefold().rstrip(os.sep) for g in guarded]
    for p in dict.fromkeys((os.path.normpath(path), _real(path))):
        if _PROTECTED.search(p.replace("\\", "/")):
            return True
        low = p.casefold()
        if any(low == g or low.startswith(g + os.sep) for g in guarded):
            return True
    return False


def _stricter(a: Decision, b: Decision) -> Decision:
    return max((a, b), key=lambda d: (_RANK[d.action], d.needs_human, d.always_ask))


def _unusable_argument(args: dict[str, object]) -> str | None:
    """Why the OS cannot take one of the arguments that name a command or a path, or None. A NUL byte reaches no
    shell or exec (it lives in the C string terminator, so a name ending there is two names and the rest vanishes);
    an argument that the locale cannot decode is not reproducible by a human at a shell. ``command`` and ``cwd``
    are checked, too."""
    for key in ("command", "cwd", "path", "file", "pattern"):
        val = args.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            try:
                val.encode("utf-8", "surrogateescape")  # rejects lone surrogates outside the escape range
            except UnicodeEncodeError:
                return f"Argument {key} is not text that can be encoded ({val!r})."
        if isinstance(val, str) and "\0" in val:
            return f"A NUL byte cannot be passed to a command or a path ({key})."
    return None


def decide(
    *,
    mode: PermissionMode | str,
    tool: str,
    args: dict[str, object] | None = None,
    cwd: str | Path = ".",
    add_dirs: list[str] | None = None,
    user_rules: list[Rule] | None = None,
    project_rules: list[Rule] | None = None,
    session_rules: list[Rule] | None = None,
    hardline_extra: list[str] | None = None,
    headless: bool = False,
    plan_for: PermissionMode | str | None = None,
) -> Decision:
    """Decide allow/ask/deny for one tool call.

    ``cwd`` is the session working dir (relative paths resolve against it);
    ``add_dirs`` extend the readable/writable roots. Order: hardline deny,
    then mode (plan/yolo), then rules (builtin < user < project < session),
    then auto-mode conversion of ask, then headless conversion of ask.

    ``plan_for``: in plan mode, the mode of the session the plan is made for (the plan-first planning turn). For an
    auto session, a read outside the roots is decided as that session decides it (see _decide_path).
    """
    if not isinstance(tool, str):
        return Decision(action="deny", message=f"Invalid tool name: {type(tool).__name__} (fail-closed).")
    try:
        mode = PermissionMode(mode)
    except ValueError:
        return Decision(action="deny", message=f"Unknown permission mode: {mode}")
    # The planning turn of an auto session reads outside the roots what the session's own turns read there: toolchain
    # and system dirs without a prompt, anything else refused with a hint. Plan mode asked a person for both, in a
    # session whose user chose not to be asked (and the turn after the plan refuses the second kind anyway).
    try:
        plan_for_mode = PermissionMode(plan_for) if plan_for is not None else None
    except ValueError:
        return Decision(action="deny", message=f"Unknown permission mode: {plan_for}")
    outside_reads = (
        PermissionMode.AUTO if mode == PermissionMode.PLAN and plan_for_mode == PermissionMode.AUTO else mode
    )
    args = args or {}
    if (bad := _unusable_argument(args)) is not None:
        return Decision(action="deny", message=bad)
    cwd_s = _norm(cwd)
    roots = [cwd_s, *[_norm(d) for d in (add_dirs or [])]]
    ruleset = merge(builtin_defaults(mode), user_rules or [], project_rules or [], session_rules or [])

    if tool == "bash":
        # The command runs in args["cwd"] (relative to the session cwd): redirects resolve against it.
        bash_cwd = _abs(str(args.get("cwd") or "."), cwd_s)
        dec = _decide_bash(mode, str(args.get("command", "")), ruleset, hardline_extra, roots, bash_cwd)
    elif tool in EDIT_TOOLS or tool in READ_TOOLS:
        raw = os.path.expanduser(str(args.get("path") or args.get("file") or "."))
        path = _abs(raw, cwd_s)
        if tool == "glob":
            path = _glob_base(path, str(args.get("pattern") or ""))
        dec = _decide_path(mode, tool, path, roots, ruleset, outside_reads)
        # The tools and the kernel follow a symlink before they apply a `..` (`link/../x` is `<target of link>/../x`),
        # `_abs` applied it first: decide the file that is really reached as well, and take the stricter answer.
        reached = _real(os.path.join(cwd_s, raw))
        if tool != "glob" and reached != _real(path):
            dec = _stricter(dec, _decide_path(mode, tool, reached, roots, ruleset, outside_reads))
        if tool in EDIT_TOOLS and dec.action != "deny" and (protected_path(path) or protected_path(reached)):
            # the edit tools run in the daemon, outside the sandbox: only a deny rule beats this, no allow rule, mode or
            # hook approval does (yolo and auto included; headless, an unattended run and a sub-agent it is a deny)
            dec.action, dec.needs_human, dec.always_ask = "ask", True, True
            dec.message = f"Edits a file that runs as code or holds credentials; confirm it yourself: {path}"
            if k3code_state(path) or k3code_state(reached):
                dec.message = f"Edits k3code's own state (trust store, memory, skills); confirm it yourself: {path}"
            if k3code_install(path) or k3code_install(reached):
                dec.message = f"Edits k3code's own install (code, launcher, service unit); confirm it yourself: {path}"
    elif tool == EXIT_PLAN_TOOL:
        dec = Decision(action="allow", patterns=["*"])
    else:
        base: Action = "deny" if mode == PermissionMode.PLAN else "ask"
        if tool in PURE_TOOLS:
            base = "allow"
        rule = evaluate(tool, "*", ruleset, default=base)
        dec = Decision(action=rule.action, patterns=["*"], rule=rule)
        if mode == PermissionMode.PLAN and tool not in PURE_TOOLS:
            dec.action, dec.message = "deny", PLAN_MSG

    return _finish(dec, mode, headless)


#: Generic headless ask-to-deny message: carries no rule name, so _finish replaces it with rule guidance below.
_HEADLESS_MSG = "Permission denied: approval required but unavailable (headless mode)."
#: Kept inside the rule-named replacement so callers matching on the headless signal still see it.
_HEADLESS_SUFFIX = "approval required but unavailable (headless mode)"


def _finish(dec: Decision, mode: PermissionMode, headless: bool) -> Decision:
    if dec.hardline is None and dec.action != "deny" and not dec.always_ask:
        if mode == PermissionMode.YOLO:
            dec.action = "allow"
        elif mode == PermissionMode.AUTO and dec.action == "ask" and not dec.needs_human:
            dec.action, dec.auto_allowed = "allow", True
    if dec.action == "ask" and headless:
        dec.action = "deny"
        dec.message = dec.message or _HEADLESS_MSG
    if dec.action == "deny" and (not dec.message or dec.message == _HEADLESS_MSG):
        # A weak model gets a bare "Denied." (or the generic headless line with no rule named) and retries the
        # same call: name the rule and what to do instead, keeping the headless signal callers match on.
        if dec.rule:
            dec.message = (
                f"Permission denied by rule {dec.rule.tool}:{dec.rule.pattern} "
                f"({_HEADLESS_SUFFIX}). Do not retry the same call; use a narrower command inside the project, "
                "or ask the user to allow it with /permissions."
            )
        elif not dec.message:
            dec.message = "Denied."
    return dec


#: Redirections that are harmless next to any command (they do not write or read an arbitrary file).
_HARMLESS_REDIRECT = re.compile(r"(?:\d*[<>]&(?:\d+|-)|&>>?\s*/dev/null|\d*>>?&?\s*/dev/null)(?![^\s;&|<>()])")
#: ``>f``, ``>>f``, ``<f``, ``&>f``, ``&>>f``, ``>&f`` (the last three send stdout+stderr to f).
_REDIRECT = re.compile(r"(?:^|[^<>&\d])(?:&>>?|\d*(?:>>?|<)&?)\s*([^\s;&|<>()]+)")
#: Output redirections only (``>``, ``>>``, ``&>``, ``&>>``, ``>&``): reading with ``<`` is never restricted by the
#: write roots.
_WRITE_REDIRECT = re.compile(r"(?:^|[^<>&\d])(?:&>>?|\d*>>?&?)\s*([^\s;&|<>()]+)")
_SUBSTITUTION = re.compile(r"\$\(|`")
#: Redirect targets that are code or credentials even inside the project.
_SENSITIVE_TARGET = re.compile(
    r"(?:^|/)(?:\.ssh|\.gnupg|\.git/(?:hooks|config)|\.k3code|\.aws|\.bash_?(?:rc|_profile)|"
    r"\.zsh(?:rc|env)|\.profile|\.zprofile|authorized_keys|\.netrc|\.npmrc|\.env)(?:/|$)",
    re.IGNORECASE,  # drvfs (/mnt/c) and macOS resolve .SSH and .ENV to the same files
)


#: The one place outside the project roots an auto-mode bash redirect may write: auto-mode bash runs in the bwrap
#: sandbox, whose ``/tmp`` is a fresh tmpfs of its own (``--tmpfs /tmp``, see sandbox.build_argv) that nothing on the
#: host sees and that is gone when the command ends; without a usable bwrap the loop asks a person before it runs a
#: command only auto mode allowed (no rule allow covers a redirect outside the roots: _voids_allow), and an unattended
#: run refuses bash. Refusing ``pytest > /tmp/out.txt 2>&1; tail /tmp/out.txt`` protected nothing.
_SANDBOX_TMP = "/tmp"


def _sandbox_scratch(path: str) -> bool:
    """``path`` (absolute, normalised) is in the sandbox's private ``/tmp``: under ``/tmp`` as written and after the
    host's symlinks (``/tmp/x -> ~/.bashrc`` stays refused, though the sandbox would not even show that link; ``..``
    was resolved, so ``/tmp/../home/u/x`` is not under it), and not under ``$HOME``: a home below ``/tmp`` has its
    ``~/.cache`` bound back writable from the host after the tmpfs."""
    if not (path.startswith(_SANDBOX_TMP + "/") and _inside(path, [_SANDBOX_TMP])):
        return False
    return not _inside(path, [os.path.expanduser("~")])


def _writes_outside_roots(sub: str, roots: list[str], cwd: str) -> bool:
    """A redirect in ``sub`` writes a file outside the project roots (and outside the sandbox's private ``/tmp``, see
    _SANDBOX_TMP). Targets that are variables or globs are left to the sandbox; the ``/dev`` sinks are not files."""
    plain = _HARMLESS_REDIRECT.sub("", sub)
    for target in _WRITE_REDIRECT.findall(plain):
        target = target.strip("'\"")
        if not target or target.startswith("/dev/") or any(ch in target for ch in "$`*?[{"):
            continue
        path = _abs(target, cwd)
        if not _inside(path, roots) and not _sandbox_scratch(path):
            return True
    return False


def _redirects_ok(plain: str, roots: list[str], cwd: str) -> bool:
    """Every redirect target is a plain path inside the project roots and not a credential/startup file."""
    for target in _REDIRECT.findall(plain):
        target = target.strip("'\"")
        if not target or any(ch in target for ch in "$*?[{~") and not target.startswith("~/"):
            return False  # a variable or glob: unknown target
        path = _abs(target, cwd)
        if not _inside(path, roots) or any(_SENSITIVE_TARGET.search(p) for p in (path, _real(path))):
            return False
    return True


def _voids_allow(
    sub: str,
    rule: Rule,
    ruleset: list[Rule],
    roots: list[str],
    cwd: str,
    heredocs: list[hardline.Heredoc] | None = None,
) -> bool:
    """A redirect or command substitution turns an allowed prefix into something else (``ls > ~/.bashrc``,
    ``git commit -m "$(evil)"``). Builtin read-only allows never cover either. A rule the user approved (session /
    project / user layer) still covers redirects into the project and substitutions whose contents are themselves
    allowed. Before, an "always allow git commit" also allowed ``git commit > ~/.bashrc`` and ``git commit -m
    "$(evil)"``. The substitutions in the body of an unquoted heredoc (``cat <<EOF``) run too: they count like those
    on the command line (``sub`` has no body)."""
    plain = _HARMLESS_REDIRECT.sub("", sub)
    has_redirect = bool(_REDIRECT.search(plain))
    body_payloads = [p for h in heredocs or () for p in h.payloads]
    has_subst = bool(_SUBSTITUTION.search(plain)) or bool(body_payloads)
    if rule.layer == 0:
        # the builtin read-only allowlist covers the project only: `cat /etc/x`, `grep -r k ~` and `ls $DIR` ask
        return (
            has_redirect
            or has_subst
            or any(_arg_outside(a, roots, cwd) for a in _path_args(sub))
            or not _readonly_argv_ok(hardline.tokens(sub))
        )
    if has_redirect and not _redirects_ok(plain, roots, cwd):
        return True
    if not has_subst:
        return False
    nested = hardline.parse(sub).nested
    for payload in body_payloads:
        inner = hardline.parse(payload)
        nested = [*nested, *inner.subs, *inner.nested]
    return not nested or any(
        evaluate("bash", n, ruleset, default="ask").action != "allow" or hardline.is_launcher(n) for n in nested
    )


class _Opts(NamedTuple):
    """The options of one builtin read-only command that are known to be safe, matched exactly."""

    flags: str = ""  # short options without a value; a cluster of them (``-la``) is fine
    valued: str = ""  # short options with a value that is no path: glued (``-n5``) or the next word
    longs: frozenset[str] = frozenset()  # ``--name``, exactly (no abbreviation, no ``=value``)
    longs_val: frozenset[str] = frozenset()  # ``--name``, ``--name=value``, exactly (the value is no path)
    words: frozenset[str] = frozenset()  # whole words as written (``find -name``)
    extra: re.Pattern[str] | None = None  # other whole spellings (``date -Iseconds``)
    glued: str = ""  # short options whose value is optional and only glued (``git diff -U5``), never the next word
    dashdash: bool = True  # ``--`` ends the options (not for find: its expression follows, primaries and all)


_DIGITS = "0123456789"


def _opts(
    flags: str = "",
    valued: str = "",
    longs: str = "",
    longs_val: str = "",
    words: str = "",
    extra: str = "",
    glued: str = "",
    dashdash: bool = True,
):
    return _Opts(
        flags,
        valued,
        frozenset(f"--{w}" for w in longs.split()),
        frozenset(f"--{w}" for w in longs_val.split()),
        frozenset(words.split()),
        re.compile(extra) if extra else None,
        glued,
        dashdash,
    )


_GIT_LOG_LONGS = (
    "stat shortstat numstat summary name-only name-status oneline no-color cached staged graph no-decorate all branches"
    " tags remotes no-merges merges first-parent abbrev-commit no-abbrev-commit patch no-patch patch-with-stat raw"
    " reverse follow full-history date-order topo-order author-date-order left-right cherry-pick boundary"
    " ignore-all-space ignore-space-change ignore-blank-lines minimal patience histogram exit-code quiet check"
    " no-ext-diff no-textconv merge-base pickaxe-regex no-prefix"
)
_GIT_LOG_VAL = (
    "format pretty date since until after before author committer grep max-count skip abbrev diff-filter unified color"
    " decorate relative word-diff word-diff-regex color-words find-renames find-copies diff-algorithm stat dirstat"
)

#: The options of the builtin read-only commands (BUILTIN_BASH_ALLOW) that are known to be safe: they print, filter or
#: format and name no file. Every other argument that starts with ``-`` makes the command ask, so an option nobody
#: listed (a list file: ``sort --files0-from=F``, ``sha256sum -c F``; a program or a write: ``jq --run-tests``,
#: ``tree -R``; a path glued to a letter: ``date -f/PATH``; a short abbreviation: ``sort --o=F``, ``tail --f``) never
#: runs unasked. ``--`` ends the options. A command with a path option is left out of the safe set on purpose (``grep
#: -f``, ``date -r``): its path would have to be checked against the roots, and `_path_args` sees only separate words.
_READONLY_OPTS: dict[str, _Opts] = {
    "ls": _opts(
        "aAbBcCdfFgGhHiklLmnNopqQrRsStuUvxX1",
        "I",
        "all almost-all human-readable classify directory reverse recursive dereference group-directories-first inode"
        " size no-group numeric-uid-gid literal hide-control-chars",
        "color sort format time time-style indicator-style ignore width",
    ),
    "cat": _opts(
        "AbeEnstTuv",
        longs="number number-nonblank show-all show-ends show-tabs squeeze-blank show-nonprinting",
    ),
    "head": _opts("qv" + _DIGITS, "nc", "quiet silent verbose", "lines bytes"),
    "tail": _opts("qv" + _DIGITS, "nc", "quiet silent verbose", "lines bytes"),
    "wc": _opts("lwcmL", longs="lines words bytes chars max-line-length"),
    "grep": _opts(
        "EFGPivwxcnhHosqIarRlLTzZ",
        "ABCme",
        "recursive dereference-recursive ignore-case no-ignore-case invert-match word-regexp line-regexp count"
        " line-number no-filename with-filename only-matching quiet silent files-with-matches files-without-matches"
        " fixed-strings extended-regexp basic-regexp perl-regexp text no-messages null null-data initial-tab binary"
        " byte-offset",
        "include exclude exclude-dir max-count after-context before-context context regexp color colour binary-files",
    ),
    "rg": _opts(
        "iIsSnNlcvwxFuaoqUHh",
        "gtTABCmeMj",
        "ignore-case smart-case case-sensitive fixed-strings files-with-matches files-without-matches count"
        " line-number no-line-number hidden no-ignore word-regexp line-regexp invert-match only-matching quiet files"
        " heading no-heading with-filename no-filename column no-column json multiline pretty",
        "glob type type-not max-count after-context before-context context regexp max-depth threads color sort"
        " max-columns",
    ),
    "find": _opts(
        words="-name -iname -type -maxdepth -mindepth -path -ipath -wholename -iwholename -size -mtime -mmin -atime"
        " -amin -ctime -cmin -newer -anewer -cnewer -empty -not -and -or -a -o -prune -print -print0 -printf -depth"
        " -xdev -mount -perm -user -group -uid -gid -executable -readable -writable -regex -iregex -regextype"
        " -lname -ilname -true -false -nouser -nogroup -links -inum -samefile -quit -daystart -ls",
        extra=r"-\d+[A-Za-z]?",
        dashdash=False,  # `find -- -files0-from F`: `--` ends -H/-L/-P only, the expression after it still runs
    ),
    "tree": _opts("adfiCnNqsDFpugxJ", "LIP", "dirsfirst noreport gitignore"),
    "file": _opts("bLhiNs", longs="brief mime mime-type mime-encoding dereference no-dereference"),
    "stat": _opts("LfFt", "c", "dereference terse file-system", "format printf"),
    "du": _opts(
        "ahscbkmxLHPS",
        "dBt",
        "all human-readable summarize total bytes apparent-size dereference one-file-system si",
        "max-depth exclude threshold block-size",
    ),
    "df": _opts(
        "hHTiklaP",
        "tx",
        "human-readable si print-type local all inodes portability total",
    ),
    "pwd": _opts("LP", longs="logical physical"),
    "which": _opts("as"),
    "type": _opts("aftpP"),
    "command": _opts("vV"),
    "id": _opts("ugGnrzZ", longs="user group groups name real zero context"),
    "uname": _opts(
        "asnrvmpio",
        longs="all kernel-name nodename kernel-release kernel-version machine processor hardware-platform"
        " operating-system",
    ),
    "date": _opts(
        "uR",
        "d",
        "utc universal rfc-email",
        "date iso-8601 rfc-3339",
        extra=r"-I(?:date|hours|minutes|seconds|ns)?",
    ),
    "sort": _opts(
        "bdfgiMhnRrsuVzcCm",
        "ktS",
        "ignore-leading-blanks dictionary-order ignore-case general-numeric-sort ignore-nonprinting month-sort"
        " human-numeric-sort numeric-sort random-sort reverse stable unique version-sort zero-terminated check merge",
        "key field-separator sort buffer-size parallel batch-size",
    ),
    "uniq": _opts(
        "cdDiuz",
        "fsw",
        "count repeated unique ignore-case zero-terminated",
        "skip-fields skip-chars check-chars",
    ),
    "cut": _opts(
        "snz",
        "bcfd",
        "only-delimited zero-terminated complement",
        "bytes characters fields delimiter output-delimiter",
    ),
    "tr": _opts("cCdst", longs="complement delete squeeze-repeats truncate-set1"),
    "jq": _opts(
        "rcesSnRCMaj" + _DIGITS,
        longs="raw-output compact-output exit-status slurp sort-keys null-input raw-input color-output"
        " monochrome-output ascii-output join-output tab stream seq",
        longs_val="indent",
    ),
    "yq": _opts(
        "rcenyYSMCaRx",
        longs="raw-output compact-output exit-status sort-keys null-input raw-input color-output monochrome-output"
        " ascii-output join-output yaml-output yaml-roundtrip xml-output tab",
        longs_val="indent width",
    ),
    "diff": _opts(
        "qsuciwbBaEZtTNyrde",
        "UCIxL",
        "brief report-identical-files ignore-case ignore-all-space ignore-space-change ignore-blank-lines text"
        " recursive new-file side-by-side ignore-trailing-space expand-tabs no-dereference strip-trailing-cr minimal",
        "unified context exclude color label",
    ),
    "cmp": _opts("bls", "ni", "verbose silent quiet print-bytes", "bytes ignore-initial"),
    "sha256sum": _opts("btz", longs="binary text zero tag untagged"),
    "sha1sum": _opts("btz", longs="binary text zero tag untagged"),
    "sha512sum": _opts("btz", longs="binary text zero tag untagged"),
    "md5sum": _opts("btz", longs="binary text zero tag untagged"),
    "basename": _opts("asz", "s", "multiple zero", "suffix"),
    "dirname": _opts("z", longs="zero"),
    "realpath": _opts(
        "emsqLPz",
        longs="canonicalize-existing canonicalize-missing no-symlinks quiet logical physical strip zero",
    ),
    "readlink": _opts(
        "fenvqsmz",
        longs="canonicalize canonicalize-existing canonicalize-missing no-newline verbose quiet silent zero",
    ),
    "git status": _opts(
        "sbvz",
        longs="short branch long ignored ahead-behind no-ahead-behind renames no-renames column no-column",
        longs_val="porcelain untracked-files ignore-submodules find-renames",
        glued="u",  # `-uno`; `-u X` is `-u` and then X
    ),
    "git diff": _opts("pusqbwaMCBRzicm" + _DIGITS, "GSn", _GIT_LOG_LONGS, _GIT_LOG_VAL, glued="U"),
    "git log": _opts("pusqbwaMCBRzicm" + _DIGITS, "GSn", _GIT_LOG_LONGS, _GIT_LOG_VAL, glued="U"),
    "git show": _opts("pusqbwaMCBRzicm" + _DIGITS, "GSn", _GIT_LOG_LONGS, _GIT_LOG_VAL, glued="U"),
    "git blame": _opts(
        "wMCefpslntb", "L", "porcelain line-porcelain incremental show-email show-name show-number root"
    ),
    "git ls-files": _opts(
        "cdmoiskuztvf",
        "x",
        "deleted modified others ignored cached stage killed unmerged directory no-empty-directory error-unmatch"
        " full-name recurse-submodules eol deduplicate exclude-standard",
        "exclude",
    ),
    "git describe": _opts(
        longs="tags all long always exact-match first-parent contains debug",
        longs_val="abbrev match exclude candidates",
    ),
    "git rev-parse": _opts(
        "q",
        longs="show-toplevel git-dir show-cdup show-prefix is-inside-work-tree is-bare-repository verify symbolic"
        " git-common-dir absolute-git-dir is-inside-git-dir quiet",
        longs_val="short abbrev-ref symbolic-full-name",
    ),
    "git remote": _opts("v", longs="verbose push all"),
}
#: jq/yq programs that read the environment (``env``, ``$ENV``, ``strenv``), load files or modules (``import``,
#: ``modulemeta``), or run a command.
#: yq's ``eval`` runs a string built at runtime (``eval("lo"+"ad_str(...)")``): no other spelling reaches those.
_QUERY_RISKY = re.compile(r"env|load|system|import|include|eval|modulemeta", re.IGNORECASE)
#: The ``make`` targets of BUILTIN_SANDBOXED_ALLOW: ``make test install`` runs every target named, so only these may.
_MAKE_TARGETS = frozenset({"test", "check", "lint"})
#: make options whose value is the next word (``-j``/``-l`` take an attached value only: ``-j install`` would hide a
#: target). ``-f``/``-C`` values are path arguments: _voids_allow keeps them inside the project.
_MAKE_VALUE_OPTS = (
    "fCIoWE",
    frozenset({"--file", "--makefile", "--directory", "--include-dir", "--old-file", "--assume-old"})
    | frozenset({"--new-file", "--assume-new", "--what-if", "--eval"}),
)
#: git subcommands that take a ``REV:path`` argument (``git show HEAD:.env``, ``git cat-file -p :.env``).
_GIT_REV_PATH = frozenset(
    {"show", "log", "diff", "blame", "cat-file", "grep", "ls-tree", "restore", "checkout", "rev-parse", "archive"}
)
#: ``git branch``/``git tag`` in list mode: (flags allowed, flags whose value is the next word, short letters allowed).
#: Anything else (``-d``, ``-m``, a new name) changes refs and asks; a pattern argument needs ``--list``/``-l``.
_REF_LIST_FLAGS = frozenset(
    {"--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
    | {"--color", "--no-color", "--column", "--no-column", "--ignore-case", "--abbrev", "--no-abbrev"}
)
_REF_LIST_VALUE = frozenset(
    {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
)
_REF_LIST: dict[str, tuple[frozenset[str], str]] = {
    "branch": (_REF_LIST_FLAGS | {"--all", "--remotes", "--verbose", "--show-current", "--quiet"}, "arlvqi"),
    "tag": (_REF_LIST_FLAGS, "li"),
}


def _opts_ok(spec: _Opts, args: list[str]) -> bool:
    """Every option in ``args`` is one ``spec`` lists, spelled exactly as listed."""
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            if spec.dashdash:
                break
            continue
        if a in spec.words or (spec.extra and spec.extra.fullmatch(a)):
            continue
        if a.startswith("--"):
            name, eq, _ = a.partition("=")
            if name in spec.longs_val or (name in spec.longs and not eq):
                continue
            return False
        if len(a) > 1 and a.startswith("-"):
            for k, ch in enumerate(a[1:], 1):
                if ch in spec.flags:
                    continue
                if ch in spec.valued:
                    if k == len(a) - 1:
                        i += 1  # its value is the next word
                    break
                if ch in spec.glued:
                    break  # its value, if any, is the rest of the word: the next word is checked on its own
                return False
    return True


def _ref_list_ok(sub: str, args: list[str]) -> bool:
    """``git branch|tag ARGS`` only lists refs."""
    allowed, letters = _REF_LIST[sub]
    listing = False
    positional = 0
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        name = a.split("=", 1)[0]
        if a.startswith("--"):
            if name not in allowed:
                return False
            listing = listing or name == "--list"
            if "=" not in a and name in _REF_LIST_VALUE and i < len(args) and not args[i].startswith("-"):
                i += 1  # its value
        elif a.startswith("-") and len(a) > 1:
            if sub == "tag" and re.fullmatch(r"-n\d*", a):
                continue
            if any(ch not in letters for ch in a[1:]):
                return False
            listing = listing or "l" in a
        else:
            positional += 1
    return positional == 0 or listing


def _readonly_argv_ok(tokens: list[str]) -> bool:
    """``tokens`` (one simple command, as written) uses only the options of a builtin read-only command that are known
    to be safe (see _READONLY_OPTS), and no query or argument that writes or runs something. Other commands pass."""
    if not tokens:
        return True
    name = os.path.basename(tokens[0])
    args = tokens[1:]
    if name == "git":
        if not args:
            return True
        sub, args = args[0], args[1:]
        if sub in _REF_LIST:
            return _ref_list_ok(sub, args)
        name = f"git {sub}"
    spec = _READONLY_OPTS.get(name)
    if spec and not _opts_ok(spec, args):
        return False
    if name == "make":
        _, positional = _options(args, *_MAKE_VALUE_OPTS)
        return all("=" in p or p in _MAKE_TARGETS for p in positional)  # `VAR=value` is an assignment
    if name == "uniq":
        # `uniq IN OUT` writes OUT
        _, positional = _options(args, "fsw", frozenset({"--skip-fields", "--skip-chars", "--check-chars"}))
        return len(positional) <= 1
    return not (name in ("jq", "yq") and any(_QUERY_RISKY.search(a) for a in args))


def _argv(
    sub: str, reads: list[str] | None = None, chdirs: list[str] | None = None, replaces: list[str] | None = None
) -> list[str]:
    """The normalised argv of ``sub`` (wrappers, assignments and redirections dropped; see hardline.normalize_argv).
    ``reads`` collects the files a dropped wrapper option reads (``xargs -a FILE``), ``chdirs`` the directories one
    runs the command in (``env -C DIR``, ``git -C DIR``), ``replaces`` the strings ``xargs -I`` fills with its input."""
    words = _REDIRECT.sub(" ", _HARMLESS_REDIRECT.sub("", sub))  # redirect targets are checked separately
    return hardline.normalize_argv(hardline.tokens(words), reads, chdirs, replaces)


def _arg_values(args: list[str]) -> list[str]:
    """The non-flag arguments in ``args``, plus the values of ``--flag=value``."""
    out: list[str] = []
    for t in args:
        if t.startswith("-"):
            if "=" in t:
                out.append(t.split("=", 1)[1])
            continue
        out.append(t)
    return out


def _path_args(sub: str) -> list[str]:
    """The non-flag arguments of ``sub`` (after sudo/env/... wrappers), plus the values of ``--flag=value``."""
    return _arg_values(_argv(sub)[1:])


def _arg_outside(arg: str, roots: list[str], cwd: str) -> bool:
    if not arg:
        return False
    if arg.startswith("$"):
        return True  # a variable: an unknown path
    return not _inside(_abs(arg, cwd), roots)


def _home_dot_path(sub: str, roots: list[str], cwd: str) -> bool:
    """``sub`` names a path below a dot folder of ``$HOME`` (``~/.config/git/credentials``,
    ``~/.cache/huggingface/token``) that a root at or above ``$HOME`` covers, i.e. not one inside a project root below
    it (``.gitignore`` there is ordinary). Credential stores live in such folders and no denylist names them all, so
    the builtin read-only allowlist does not cover these reads. Checked on the path as written and on its realpath (a
    symlink in ``$HOME`` into a dot folder); a short option with a dot (``-f.config/x``) may attach such a path."""
    home = os.path.normpath(os.path.expanduser("~"))
    homes = list(dict.fromkeys((home, _real(home))))
    if not _inside(home, roots):
        return False  # no root covers $HOME: a path there is outside the roots, which voids a builtin allow already
    projects = [r for r in roots if not _inside(home, [r])]
    for t in _argv(sub)[1:]:
        if t.startswith("-"):
            if "=" in t:
                t = t.split("=", 1)[1]
            elif not t.startswith("--") and "." in t:
                return True
            else:
                continue
        if not t or t.startswith("$"):
            continue
        lexical = _abs(t, cwd)
        for p in dict.fromkeys((lexical, _real(os.path.join(cwd, os.path.expanduser(t))))):
            if projects and _inside(p, projects):
                continue
            for h in homes:
                if p.startswith(h.rstrip("/") + "/") and any(c.startswith(".") for c in p[len(h) :].split("/")):
                    return True
    return False


# ── credential files named on a command line ──

#: ``<file`` (not ``<<``, ``<<<``, ``<&``, ``<(``, ``<>``): a file the command reads.
_INPUT_REDIRECT = re.compile(r"(?:^|[^<>&\d])\d*<(?![<&(>])\s*([^\s;&|<>()]+)")
_ANY_VAR = re.compile(r"\$(?:\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])")
_GLOB_CHARS = frozenset("*?[")
#: Print, test or change the metadata of the paths they are given, never a file's content: ``echo .env``,
#: ``test -f .env``, ``chmod 600 ~/.ssh/id_rsa``, ``ssh-add ~/.ssh/id_ed25519``. Exempt unless their output goes on to a
#: pipe or a substitution, where a printed name feeds a reader (``realpath .env | xargs cat``, ``tac $(echo .env)``).
_NAME_ONLY = frozenset(
    {
        "echo",
        "printf",
        "ls",
        "realpath",
        "readlink",
        "stat",
        "file",
        "chmod",
        "chown",
        "chgrp",
        "touch",
        "test",
        "[",
        "[[",
        "ssh-add",
    }
)
#: ``file`` options whose value it reads: ``-f`` lists files to check (each line comes back as a name), ``-m`` names a
#: magic file (bad lines come back in the warnings).
_FILE_VALUE_OPTS = ("eFfmP", frozenset({"--exclude", "--separator", "--files-from", "--magic-file", "--parameter"}))
_FILE_READ_OPTS = frozenset({"-f", "-m", "--files-from", "--magic-file"})
#: ssh client short options taking a value. ``-i KEY``, ``-o IdentityFile=KEY`` and ``-F CONFIG`` name a file the
#: client authenticates with or reads its settings from (see _ssh_key_value). (rsync is not here: its ``-i`` is
#: ``--itemize-changes``, so the next word is a source it copies; an rsync key goes in ``-e 'ssh -i KEY'``, a command
#: line (hardline.ssh_option_commands returns it) that _secret_access checks as an ssh command of its own.)
_SSH_VALUE_OPTS = hardline.SSH_VALUE_OPTS
_IDENTITY_OPTION = re.compile(r"identityfile=", re.IGNORECASE)
_SSH_CONFIG_NAME = re.compile(r"(?:^|/)(?:config|[^/]*(?:\.conf|_config))$")
#: ``ssh-keygen -l`` (fingerprint) and ``-y`` (the public half) read the key named by ``-f`` and print no secret; any
#: other mode letter (``-p``, ``-e``, ``-i``, ``-t`` ...) leaves the key file classified as a read.
_KEYGEN_VALUE_OPTS = "abCDEFfGIJjKMmNnOPRrSstVwYZz"
_KEYGEN_PRINT_ONLY = frozenset("lyvqfE")
#: curl options that send a file: ``-d @FILE`` (and ``--data-urlencode NAME@FILE``), ``-F NAME=@FILE``/``NAME=<FILE``,
#: ``-T FILE``. ``--data-raw @x`` sends the text ``@x``.
_CURL_VALUE_OPTS = "AbcCdDeEFHKmoPQrtTuUwxXyYz"
_CURL_DATA = frozenset({"-d", "--data", "--data-ascii", "--data-binary", "--data-urlencode", "--json"})
_CURL_FORM = frozenset({"-F", "--form"})
_CURL_LONG_VALUE = _CURL_DATA | _CURL_FORM | {"--upload-file", "--config"}
_CURL_FORM_FILE = re.compile(r"=[@<]([^;]*)")
#: Delete a credential file: a person confirms.
_REMOVERS = frozenset({"rm", "unlink", "shred"})
#: ``SOURCE... DEST``: a credential file as the source is copied somewhere readable (deny); as the destination it is
#: only overwritten (ask).
_COPIERS = frozenset({"cp", "mv", "ln", "install"})
_KEY_NAMES = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
#: ``human``: a variable may point at a credential file; ``confirm``: the command deletes, sources, stages or hands a
#: container one. Both are an ask that a person answers. ``unchecked``: a glob was too large to check for credential
#: files; a person is asked, and yolo (where nobody can be) refuses. ``unchecked_w``: the same for a glob a command
#: deletes or overwrites, which yolo has always let through.
_SEVERITY = {"ask": 1, "human": 2, "confirm": 2, "unchecked_w": 2, "unchecked": 3, "deny": 4}


def _worse(a: str | None, b: str | None) -> str | None:
    return b if b and (a is None or _SEVERITY[b] > _SEVERITY[a]) else a


def _dotenv_name(name: str) -> bool:
    """A dotenv file (not a committed template). Compared case-insensitively: ``.ENV`` is the same file on drvfs
    (``/mnt/c``) and macOS."""
    name = name.lower()
    return name == ".env" or (name.startswith(".env.") and not name.endswith(_DOTENV_TEMPLATES))


def _sensitive_name(text: str) -> bool:
    """``text`` (a path with its unknown parts removed) names a credential: a dotenv file, an ssh key, a secrets dir,
    a process environment."""
    lowered = text.lower()  # case-insensitive file systems (drvfs, macOS): .SSH/ID_RSA is ~/.ssh/id_rsa
    parts = [p for p in lowered.split("/") if p]
    if not parts:
        return False
    if any(p in (".ssh", ".gnupg", ".aws") for p in parts) or "/.config/k3code/" in f"/{lowered}/":
        return True
    base = parts[-1]
    return _dotenv_name(base) or base in _KEY_NAMES or base == "environ" or base.endswith((".pem", ".key"))


def _glob_match(pattern: str, path: str) -> bool:
    """Shell-style match of an absolute glob against an absolute path: component by component (``*`` never crosses a
    ``/``) and a wildcard never matches a leading dot (``cat *`` does not read ``.env``). Case-insensitive, as on drvfs
    and macOS (``cat .EN?`` reads ``.env`` there)."""
    pat, parts = pattern.split("/"), path.split("/")
    if len(pat) != len(parts):
        return False
    return all(
        fnmatch.fnmatchcase(name.lower(), comp.lower()) and (comp.startswith(".") or not name.startswith("."))
        for comp, name in zip(pat, parts, strict=True)
    )


def _canonical_secrets(pattern: str, home: str) -> list[str]:
    """Representative credential paths a glob is matched against (it need not exist on this machine)."""
    keys = [*_KEY_NAMES, "x.pem", "x.key", "config", "authorized_keys"]
    out = [os.path.join(home, ".ssh", k) for k in keys]
    out += [os.path.join(home, ".gnupg", "x"), os.path.join(home, ".aws", "credentials")]
    out += [os.path.join(d, "env") for d in _secret_dirs()]
    out += ["/proc/self/environ", "/proc/1/environ", "/proc/self/task/1/environ"]
    folder = os.path.dirname(pattern)  # dotenv files live anywhere: try them next to the pattern
    out += [os.path.join(folder, n) for n in (".env", ".env.local", ".env.production")]
    return out


#: Bounds of the on-disk walk of a glob, shared by every glob word of one bash decision: directory entries read and
#: seconds spent. A pattern that matches nothing never yields a hit, so a cap on hits would not stop it; the walk runs
#: on the daemon's event loop. ``_GLOB_MAX_WILD`` caps the wildcard components of one pattern (the walk recurses once
#: for each); a pattern with more is not walked.
_GLOB_MAX_ENTRIES = 20_000
_GLOB_BUDGET_S = 0.5
_GLOB_MAX_WILD = 32


class _GlobBudget:
    def __init__(self) -> None:
        self.entries = 0
        self.deadline = time.monotonic() + _GLOB_BUDGET_S
        self.spent = False
        self._dirs: list[str] | None = None

    @property
    def secret_dirs(self) -> list[str]:
        """The secret dirs, resolved once for the decision instead of once for every hit."""
        if self._dirs is None:
            self._dirs = _folded_secret_dirs()
        return self._dirs

    def charge(self) -> bool:
        """Count one directory entry; False once the entry cap or the time budget is used up."""
        self.entries += 1
        if self.entries > _GLOB_MAX_ENTRIES or (self.entries % 64 == 0 and time.monotonic() > self.deadline):
            self.spent = True
        return not self.spent


def _glob_walk(base: str, parts: list[str], budget: _GlobBudget) -> Iterator[str]:
    """The paths ``glob.glob`` would find for ``base`` + ``parts`` (no ``**``, a wildcard skips dot names), stopping
    when ``budget`` runs out. Literal components are joined in a loop; only a wildcard component recurses."""
    if budget.spent:
        return
    k = 0
    while k < len(parts) and not _GLOB_CHARS & set(parts[k]):
        base = os.path.join(base, parts[k])
        k += 1
    if k == len(parts):
        if os.path.lexists(base):
            yield base
        return
    comp, rest = parts[k], parts[k + 1 :]
    try:
        with os.scandir(base) as it:
            for entry in it:
                if not budget.charge():
                    return
                name = entry.name
                if (name.startswith(".") and not comp.startswith(".")) or not fnmatch.fnmatchcase(name, comp):
                    continue
                try:
                    if rest and not entry.is_dir():
                        continue
                except OSError:  # a symlink that cannot be followed: glob skips it too
                    continue
                yield from _glob_walk(os.path.join(base, name), rest, budget)
    except OSError:
        return


def _glob_reads_secret(pattern: str, home: str, budget: _GlobBudget) -> bool | None:
    """An absolute glob that matches a credential file: on disk, by its literal leading directory, or by its shape
    against the canonical secret names. None when none of these says so and the on-disk walk did not finish within
    ``budget`` (or was not tried: the budget was spent already, or the pattern is too deep): the answer is unknown."""
    parts = pattern.split("/")[1:]
    if sum(1 for p in parts if _GLOB_CHARS & set(p)) > _GLOB_MAX_WILD:
        budget.spent = True
    for hit in _glob_walk("/", parts, budget):
        if sensitive_path(hit, budget.secret_dirs):
            return True
    literal: list[str] = []
    for seg in pattern.split("/"):
        if _GLOB_CHARS & set(seg):
            break
        literal.append(seg)
    base = "/".join(literal) or "/"
    if base != "/" and sensitive_path(base, budget.secret_dirs):
        return True
    if any(_glob_match(pattern, c) for c in _canonical_secrets(pattern, home)):
        return True
    return None if budget.spent else False


_BRACE = re.compile(r"\{([^{}]*,[^{}]*)\}")


def _brace_expand(word: str, limit: int = 64) -> list[str]:
    """The words the shell makes of ``a{b,c}d`` (``abd``, ``acd``); capped so a pathological word cannot explode."""
    out = [word]
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
        out = nxt[:limit]
        if not changed:
            break
    return out


@dataclass(frozen=True)
class _Where:
    """The directories a relative path on a command line may be relative to: the session directory and every directory
    a ``cd``/``pushd`` on the line can have moved to (see _where). ``unknown``: one of them could not be worked out
    (``cd $DIR``, ``cd -``, a glob), so a relative path could be read from anywhere."""

    dirs: tuple[str, ...]
    unknown: bool = False
    home_moved: bool = False  # ``HOME`` is assigned on the line: ``~`` and ``$HOME`` may not mean the daemon's home
    replaces: tuple[str, ...] = ()  # strings a program on the line fills with its input (see _CdScan.replaces)
    links: tuple[str, ...] = ()  # names of symbolic links the line makes (see _CdScan.link_names)


#: ``~``, ``$HOME`` and ``${HOME...}`` at the start of a word: the home directory as the shell will spell it.
_HOME_WORD = re.compile(r"~(?=/|$)|\$HOME(?![A-Za-z0-9_])|\$\{HOME\b")


def _placeholder_credential(word: str, where: _Where, home: str) -> bool:
    """``word`` holds a string the line fills with its input (``~/{}`` under ``xargs -I{}``, ``{}/id_rsa`` under ``find
    -exec``) in a place that could make it a credential: below the home directory, a credential folder or one that holds
    one (the same folders ``_may_be_credential`` tries), or in front of a credential name. A bare placeholder
    (``cat {}``) is left alone, as is one below a folder that holds no credential (``sub/{}``)."""
    word = word.strip("'\"")
    secret = _secret_dirs()
    parents = {home, "/", "/proc/self", *(os.path.dirname(d) for d in secret)}
    for r in where.replaces:
        before, found, after = word.partition(r)
        if not found:
            continue
        if after.startswith("/") and _may_be_credential(after.lstrip("/"), home):
            return True
        if not before:
            continue
        for cwd in where.dirs:
            expanded = hardline.expand_vars(before, cwd, home)
            if "$" in expanded or "`" in expanded:
                continue
            folder = _abs(expanded if expanded.endswith("/") else os.path.dirname(expanded) or ".", cwd)
            if folder in parents or any(folder == d or folder.startswith(d + "/") for d in secret):
                return True
    return False


def _crosses_link(word: str, cwd: str, home: str, links: tuple[str, ...]) -> bool:
    """``word`` goes through a symbolic link the line makes and goes on to a name that may be a credential."""
    word = hardline.expand_vars(word.strip("'\""), cwd, home)
    if not word or "$" in word or "`" in word:
        return False
    path = _abs(word, cwd)
    for link in links:
        base = _abs(hardline.expand_vars(link, cwd, home), cwd)
        if path.startswith(base.rstrip("/") + "/") and _may_be_credential(path[len(base) :].lstrip("/"), home):
            return True
    return False


def _classify_arg(arg: str, where: _Where, home: str, budget: _GlobBudget | None = None) -> str | None:
    """``deny`` when ``arg`` names (or globs onto) a credential file, ``human`` when an unknown variable stands in
    front of a credential name (``$DIR/.env``) or an unknown directory may hold it, ``unchecked`` when a glob could
    not be checked within ``budget``, else None. ``{a,b}`` alternatives are classified one by one, each against every
    directory in ``where``."""
    budget = budget or _GlobBudget()
    worst: str | None = None
    for word in _brace_expand(arg):
        for cwd in where.dirs:
            worst = _worse(worst, _classify_word(word, cwd, home, budget))
            if where.links and _crosses_link(word, cwd, home, where.links):
                worst = _worse(worst, "human")  # ``ln -s ~ h && head h/.ssh/id_rsa``: the link is not on disk yet
        if where.unknown and _relative_word(word) and _may_be_credential(word, home):
            worst = _worse(worst, "human")
        if where.home_moved and _HOME_WORD.match(word.strip("'\"")):
            worst = _worse(worst, "human")  # ``HOME=~/.ssh; cat ~/key``: any name in a home that is not the daemon's
        if where.replaces and _placeholder_credential(word, where, home):
            worst = _worse(worst, "human")
    return worst


def _relative_word(word: str) -> bool:
    word = word.strip("'\"")
    return bool(word) and "://" not in word and not word.startswith(("/", "~", "$"))


#: What a file in a credential folder is called: reading a bare one of these after a ``cd`` to somewhere unknown may be
#: reading the credential (the other names in those folders are not worth a question: every ``cat README.md`` after a
#: ``cd "$(mktemp -d)"`` would ask).
_CREDENTIAL_LEAVES = frozenset(
    {
        "credentials",
        "config",
        "env",
        "authorized_keys",
        "known_hosts",
        "private-keys-v1.d",
        "pubring.kbx",
        "trustdb.gpg",
    }
)


def _may_be_credential(word: str, home: str, budget: _GlobBudget | None = None) -> bool:
    """A relative ``word`` that names a credential from some directory a ``cd`` to somewhere unknown can reach: a
    credential leaf name, or a path that is one from the home directory, the root, a process directory or the parent
    of a credential folder (``.ssh/id_rsa``, ``k3code/env``, ``proc/self/environ``)."""
    if _sensitive_name(word) or os.path.basename(word.strip("'\"")).lower() in _CREDENTIAL_LEAVES:
        return True
    parents = (os.path.dirname(d) for d in _secret_dirs())
    return any(
        _classify_word(word, probe, home, budget) for probe in dict.fromkeys([home, "/", "/proc/self", *parents])
    )


def _classify_word(arg: str, cwd: str, home: str, budget: _GlobBudget | None = None) -> str | None:
    arg = arg.strip("'\"")
    if not arg or "://" in arg:
        return None
    word = hardline.expand_vars(arg, cwd, home)
    if "$" in word or "`" in word:
        return "human" if _sensitive_name(_ANY_VAR.sub("", word)) else None
    path = _abs(word, cwd)
    if _GLOB_CHARS & set(word):
        reads = _glob_reads_secret(path, home, budget or _GlobBudget())
        return "unchecked" if reads is None else "deny" if reads else None
    return "deny" if sensitive_path(path) else None


def _target_dir_flag(args: list[str]) -> bool:
    """``cp -t DIR SRC...`` / ``--target-directory``: the destination is the flag value; the rest are sources."""
    for a in args:
        if a == "--":
            return False
        if a.startswith("--target-directory") or (a.startswith("-") and not a.startswith("--") and "t" in a[1:]):
            return True
    return False


def _options(args: list[str], short_val: str, long_val: frozenset[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """GNU-style scan of ``args``: every option with its value (``""`` for a flag; short clusters split, ``-xVALUE``
    and ``--opt=value`` included) and the positional arguments. Options may follow positionals; ``--`` ends them."""
    opts: list[tuple[str, str]] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            positional += args[i:]
            break
        if a.startswith("--"):
            key, eq, value = a.partition("=")
            if not eq and key in long_val and i < len(args):
                value, i = args[i], i + 1
            opts.append((key, value))
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch in short_val:
                    value = a[k + 1 :]
                    if not value and i < len(args):
                        value, i = args[i], i + 1
                    opts.append(("-" + ch, value))
                    break
                opts.append(("-" + ch, ""))
        else:
            positional.append(a)
    return opts, positional


def _words(args: list[str]) -> list[str]:
    """Every argument that may name a file: the non-flag ones, ``--flag=value`` values and a value glued to a short
    option (``-T.env``, ``-f~/.ssh/id_rsa``)."""
    glued = [a[2:] for a in args if a.startswith("-") and not a.startswith("--") and len(a) > 2 and "=" not in a]
    return _arg_values(args) + glued


def _curl_reads(args: list[str]) -> list[str]:
    """curl's arguments with the files its data, form and upload options send spelled as plain paths."""
    opts, positional = _options(args, _CURL_VALUE_OPTS, _CURL_LONG_VALUE)
    reads = list(positional)
    for opt, value in opts:
        if opt in _CURL_DATA and "@" in value:
            value = value.split("@", 1)[1]
        elif opt in _CURL_FORM and (m := _CURL_FORM_FILE.search(value)):
            value = m.group(1)
        reads.append(value)
    return reads


def _copier_roles(args: list[str]) -> tuple[list[str], list[str]]:
    """(sources, destination) of ``cp``/``mv``/``ln``/``install``."""
    reads = _arg_values(args)
    writes: list[str] = []
    positional = [a for a in args if not a.startswith("-")]
    if _target_dir_flag(args):
        for k, a in enumerate(args[:-1]):
            if a in ("-t", "--target-directory") and args[k + 1] in reads:
                writes = [args[k + 1]]
                reads.remove(args[k + 1])
                break
    elif len(positional) >= 2:
        writes = [positional[-1]]
        del reads[len(reads) - 1 - reads[::-1].index(positional[-1])]
    return reads, writes


def _ssh_key_value(opt: str, value: str) -> bool:
    """``-i KEY`` / ``-o IdentityFile=KEY`` (a key that fails to load prints nothing of itself), or ``-F`` naming a
    config file: ssh echoes the first word of every line it cannot parse, so ``-F ~/.ssh/id_rsa`` prints the key."""
    if opt == "-i" or (opt == "-o" and _IDENTITY_OPTION.match(value)):
        return True
    return opt == "-F" and bool(_SSH_CONFIG_NAME.search(value))


def _credential_roles(name: str, args: list[str], piped: bool) -> tuple[list[str], list[str], list[str]]:
    """The arguments of ``name args`` that may name a credential file, by what the command does with it: (reads or
    sends it: deny, overwrites it: ask, deletes/sources/stages it or hands it to a container: confirm). An argument the
    command only prints, tests, changes the metadata of or authenticates with is in none of the lists; any command not
    named here reads every argument."""
    if name in _NAME_ONLY or (name == "git" and args[:1] == ["check-ignore"]):
        if piped:
            return _words(args), [], []
        if name == "file":
            opts, _ = _options(args, *_FILE_VALUE_OPTS)
            return [v for o, v in opts if o in _FILE_READ_OPTS], [], []
        return [], [], []
    if name in _SSH_VALUE_OPTS:
        opts, positional = _options(args, _SSH_VALUE_OPTS[name], frozenset())
        # an -o XxxCommand value is a command line, not a path: _sub_secret_access checks it
        paths = [
            v
            for o, v in opts
            if v and not _ssh_key_value(o, v) and not (o == "-o" and hardline.ssh_command_value(v) is not None)
        ]
        return paths + positional, [], []
    if name == "ssh-keygen":
        opts, positional = _options(args, _KEYGEN_VALUE_OPTS, frozenset())
        letters = {o[1] for o, _ in opts}
        if letters & {"l", "y"} and letters <= _KEYGEN_PRINT_ONLY:
            return positional, [], []
        return _words(args), [], []
    if name == "curl":
        return _curl_reads(args), [], []
    if name in _REMOVERS:
        return [], [], _words(args)
    if name in ("source", "."):
        words = _words(args)
        return words[1:], [], words[:1]
    if name == "git" and args[:1] in (["add"], ["rm"], ["mv"]):
        opts, positional = _options(args[1:], "", frozenset({"--pathspec-from-file", "--chmod"}))
        return [v for o, v in opts if v], [], positional
    if name in ("docker", "podman"):
        env_files: list[str] = []
        rest: list[str] = []
        i = 0
        while i < len(args):
            if args[i] == "--env-file" and i + 1 < len(args):
                env_files.append(args[i + 1])
                i += 2
                continue
            if args[i].startswith("--env-file="):
                env_files.append(args[i].split("=", 1)[1])
            else:
                rest.append(args[i])
            i += 1
        return _words(rest), [], env_files
    if name in _COPIERS:
        reads, writes = _copier_roles(args)
        return reads, writes, []
    words = _words(args)
    if name == "git" and args[:1] and args[0] in _GIT_REV_PATH:
        # `git show HEAD:.env.production`, `git cat-file -p :.env`: the path half of a REV:path is read too
        words += [w.split(":", 1)[1] for w in words if ":" in w and "://" not in w and w.split(":", 1)[1]]
    return words, [], []


#: ``cd`` and ``pushd`` change the directory the rest of the line reads from; ``popd`` and ``pushd`` with no
#: directory only go back to one the line was already in.
_CD_BUILTINS = frozenset({"cd", "pushd"})
#: the most directories one command line is followed into; past it the directory counts as unknown
_CD_MAX_DIRS = 32
#: how many times the ``cd``s of a line with a loop or a function are replayed on the directories found so far (a loop
#: starts its second turn in the directory its first one ended in); one still moving after that has an unknown directory
_CD_PASSES = 3
_LOOP = re.compile(r"(?:^|[\s;&|(])(?:for|while|until|select|function)\b|\(\)\s*\{")


def _loops(parsed: hardline.Parsed) -> bool:
    return bool(_LOOP.search(parsed.clean)) or any(_loops(child) for child in parsed.children)


#: Words that open a compound command in front of the command it runs: ``if cd ~; then ...``, ``{ cd ~; }``,
#: ``! cd ~``, ``do cd ~``.
_SHELL_KEYWORDS = frozenset({"!", "{", "(", "if", "then", "elif", "else", "do", "while", "until"})
#: Commands that run text this check never reads: a sourced file, an alias, a trap.
_RUNS_UNREAD_TEXT = frozenset({"source", ".", "alias", "trap"})
#: Shell settings that move the directory in ways the check does not follow: a rewritten ``DIRSTACK`` that ``popd``
#: returns to, ``cdable_vars`` (``cd x`` goes to the directory ``$x`` names).
_CD_SETTINGS = ("DIRSTACK", "cdable_vars")
_HOME_NAME = re.compile(r"HOME(?:\[[^\]]*\])?")
_HOME_ASSIGNMENT = re.compile(_HOME_NAME.pattern + r"\+?=")
_HOME_NAMEREF = re.compile(r"[A-Za-z_]\w*=" + _HOME_NAME.pattern)
#: Commands that set the variables named among their arguments (``read HOME``), apart from ``printf -v NAME``.
_SETS_NAMED = frozenset({"read", "mapfile", "readarray", "getopts"})
#: Commands whose ``-C DIR``/``--directory DIR`` runs them in DIR: the short options of each that take a value, the
#: short option that names the directory, and the long options that do. getopt takes any unambiguous prefix of a long
#: option (``make --di DIR``), so a prefix of four characters or more counts, and so does an ambiguous one: it only
#: makes the check look at a directory the program refuses. bsdtar has its own table (``-H`` and ``-L`` are flags,
#: ``-s`` and ``-W`` take a value, ``--cd`` names the directory); in make ``-L`` is a flag.
_DIR_OPTION_COMMANDS = {
    "tar": ("fCTXbHIgFVLNK", "C", ("--directory",)),
    "gtar": ("fCTXbHIgFVLNK", "C", ("--directory",)),
    "bsdtar": ("bCfIsTWX", "C", ("--directory", "--cd")),
    "make": ("CfIoWE", "C", ("--directory",)),
    "gmake": ("CfIoWE", "C", ("--directory",)),
    "cpio": ("CDEFHIMOR", "D", ("--directory",)),
    "uv": ("", "", ("--directory",)),
    "cmake": ("", "", ()),  # ``-E chdir DIR CMD...``
}
_TAR_COMMANDS = frozenset({"tar", "gtar", "bsdtar"})
#: Programs that put their input after the arguments they are given, which can hand a directory option its value.
_FEEDERS = frozenset({"xargs", "parallel"})
#: ``find -exec`` and friends fill ``{}`` with a file name; GNU ``parallel`` fills these.
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_PARALLEL_REPLACES = (
    "{}",
    "{.}",
    "{/}",
    "{//}",
    "{/.}",
    "{#}",
    "{%}",
    "{=",
    *(f"{{{n}}}" for n in range(1, 10)),
)
#: ``git`` subcommands that write files into the work tree, links included.
_GIT_WRITES_TREE = frozenset(
    {"checkout", "switch", "restore", "reset", "merge", "pull", "clone", "rebase", "apply", "am", "cherry-pick"}
)


@dataclass
class _CdScan:
    """The directory changes on a command line: each ``cd``/``pushd`` as (name, arguments), and what makes the
    directories reachable unclear: ``CDPATH`` set (a relative ``cd`` may go elsewhere), or ``unknown`` (a command the
    check cannot read as a command word, ``HOME`` rewritten, a setting from _CD_SETTINGS). ``home``: ``HOME`` is
    assigned. ``links``: the line makes a symbolic link or unpacks files (an archive, a checkout), which a later
    ``cd`` can pass through; ``link_names`` are the names of the links it makes by ``ln -s``/``cp -s``. ``replaces``:
    the strings a program on the line fills with its input (``xargs -I``, ``find -exec``'s ``{}``, ``parallel``'s
    ``{}``), which a ``cd`` operand must not hold. ``tar_options``, ``tar``: ``TAR_OPTIONS`` is mentioned, tar runs."""

    found: list[tuple[str, list[str]]] = field(default_factory=list)
    cdpath: bool = False
    unknown: bool = False
    home: bool = False
    links: bool = False
    link_names: list[str] = field(default_factory=list)
    replaces: list[str] = field(default_factory=list)
    tar_options: bool = False
    tar: bool = False


def _dir_option_values(name: str, args: list[str]) -> list[str]:
    """The directories ``tar -C DIR``, ``--directory DIR``, ``make -C DIR``, ``cpio -D DIR``, ``uv --directory DIR`` and
    ``cmake -E chdir DIR`` run in (an old-style ``tar cfC x DIR`` bundle and an abbreviated ``--dir`` included)."""
    short_val, short_dir, long_dirs = _DIR_OPTION_COMMANDS[name]
    if name == "cmake":
        return [args[2]] if len(args) > 2 and args[0] == "-E" and args[1] == "chdir" else []
    values: list[str] = []
    if name in _TAR_COMMANDS and args and not args[0].startswith("-"):
        rest = args[1:]  # an old-style bundle: each letter that takes a value takes the next word, in order
        for ch in args[0]:
            if ch in short_val and rest:
                if ch == short_dir:
                    values.append(rest[0])
                rest = rest[1:]
        args = rest
    spelled: list[str] = []
    for a in args:
        key, eq, value = a.partition("=")
        full = next((o for o in long_dirs if a.startswith("--") and len(key) >= 4 and o.startswith(key)), None)
        spelled.append(full + eq + value if full else a)
    opts, _ = _options(spelled, short_val, frozenset(long_dirs))
    return values + [value for opt, value in opts if value and (opt == "-" + short_dir or opt in long_dirs)]


def _dangling_dir_option(argv: list[str], last: str) -> bool:
    """A directory option ends the command (``xargs tar -cf - -C``): the program that feeds it hands it its value."""
    if argv[0] not in _DIR_OPTION_COMMANDS and argv[0] not in ("git", "env"):
        return False
    if last.startswith("--"):
        return "=" not in last and len(last) >= 4 and ("--directory".startswith(last) or "--chdir".startswith(last))
    return last.startswith("-") and last[-1] in "CD"


def _parallel_replaces(raw: list[str]) -> list[str]:
    """The strings GNU parallel swaps for its input in the command it runs: ``{}`` and its relatives, and what
    ``-I``/``--replace`` name."""
    if "parallel" not in {os.path.basename(t) for t in raw}:
        return []
    out = list(_PARALLEL_REPLACES)
    for k, t in enumerate(raw):
        if t in ("-I", "--replace") and k + 1 < len(raw):
            out.append(raw[k + 1])
        elif t.startswith("--replace="):
            out.append(t.partition("=")[2])
        elif t.startswith("-I") and len(t) > 2:
            out.append(t[2:])
    return out


def _makes_symlink(name: str, args: list[str]) -> bool:
    """``ln -s``, ``cp -s`` (also ``--symbolic``/``--symbolic-link``, clustered or abbreviated)."""
    if name not in ("ln", "cp"):
        return False
    for a in args:
        if a == "--":
            break
        if a.startswith("--"):
            if len(a) >= 4 and "--symbolic-link".startswith(a.partition("=")[0]):
                return True
        elif a.startswith("-") and "s" in a[1:]:
            return True
    return False


def _link_names(args: list[str]) -> list[str]:
    """Where an ``ln -s``/``cp -s`` may put its links: the last operand (and below it, when that is a folder), or the
    ``-t`` folder."""
    opts, positional = _options(args, "St", frozenset({"--suffix", "--target-directory"}))
    positional = [os.path.expanduser(p.replace("${HOME}", "~").replace("$HOME", "~")) for p in positional]
    folder = next((v for o, v in opts if o in ("-t", "--target-directory") and v), None)
    if folder is not None:
        return [os.path.join(folder, os.path.basename(p)) for p in positional]
    if len(positional) == 1:
        return [os.path.basename(positional[0].rstrip("/")) or "."]
    names = positional[-1:]
    return names + [os.path.join(positional[-1], os.path.basename(p.rstrip("/"))) for p in positional[:-1]]


def _unpacks(name: str, args: list[str]) -> bool:
    """The command may write symbolic links into the tree: unpacking an archive (``tar x``, ``unzip``) or a git command
    that writes the work tree."""
    if name in _TAR_COMMANDS:
        if args and not args[0].startswith("-"):
            return "x" in args[0]
        for a in args:
            if a.startswith("--"):
                if len(a) >= 4 and any(o.startswith(a.partition("=")[0]) for o in ("--extract", "--get")):
                    return True
            elif a.startswith("-") and "x" in a[1:]:
                return True
        return False
    if name == "git":
        return bool(args) and args[0] in _GIT_WRITES_TREE
    return name == "unzip"


def _assigns_home(raw: list[str]) -> bool:
    """``HOME`` is set by this simple command: ``HOME=x`` (``HOME[0]=x``, ``HOME+=x``, behind ``export``/``declare``),
    ``printf -v HOME``, ``read HOME`` and its kin, ``for``/``select HOME in``, or a name reference to it
    (``declare -n h=HOME``, after which ``h=x`` sets it)."""
    if any(_HOME_ASSIGNMENT.match(t) for t in raw):
        return True
    named = [t for t in raw if _HOME_NAME.fullmatch(t)]
    if "printf" in raw and any(t == "-v" and i + 1 < len(raw) and raw[i + 1] in named for i, t in enumerate(raw)):
        return True
    if "printf" in raw and any(t.startswith("-v") and _HOME_NAME.fullmatch(t[2:]) for t in raw):
        return True
    if named and any(t in _SETS_NAMED for t in raw):
        return True
    if named and any(a in ("for", "select") and b in named for a, b in zip(raw, raw[1:], strict=False)):
        return True
    ref = any(t in ("declare", "typeset", "local") for t in raw) and any(re.fullmatch(r"-\w*n\w*", t) for t in raw)
    return ref and any(_HOME_NAMEREF.fullmatch(t) for t in raw)


def _cd_commands(parsed: hardline.Parsed, scan: _CdScan | None = None) -> _CdScan:
    """Every directory change on the line, nested commands (``bash -c``, ``$(...)``, a subshell, a function body)
    included: ``cd``, ``pushd`` (``pushd -n DIR`` too: ``popd`` comes back to it) and the directory an ``env -C``,
    ``sudo -D`` or ``git -C`` runs its command in, and the one ``tar -C`` and ``make -C`` work in. A ``trap``
    action runs later (at exit, on a signal), not now, so its ``cd`` does not move the rest of the line: its
    payload is still checked for secrets in _secret_access, but skipped here."""
    scan = scan or _CdScan()
    trap_payloads: set[str] = set()
    for sub in parsed.subs:
        argv0 = _argv(sub)
        if argv0 and argv0[0] == "trap":
            trap_payloads.update(hardline._trap_actions(argv0[1:]))
    for sub in parsed.subs:
        scan.cdpath = scan.cdpath or "CDPATH" in sub
        scan.unknown = scan.unknown or any(name in sub for name in _CD_SETTINGS)
        scan.tar_options = scan.tar_options or "TAR_OPTIONS" in sub
        raw = hardline.tokens(sub)
        homed = _assigns_home(raw)
        scan.home = scan.home or homed
        scan.unknown = scan.unknown or homed
        chdirs: list[str] = []
        argv = _argv(sub, None, chdirs, scan.replaces)
        scan.found += [("cd", [d]) for d in chdirs]
        while argv and (argv[0] in _SHELL_KEYWORDS or argv[0] == "function" or argv[0] in ("builtin", "command")):
            argv = argv[2:] if argv[0] == "function" else argv[1:]  # ``function NAME {``: the name is no command
        while len(argv) > 1 and argv[0] == "busybox":
            argv = argv[1:]  # ``busybox tar -C DIR``: the applet is the command
        if not argv:
            continue
        if "$" in argv[0] or "`" in argv[0] or argv[0] in _RUNS_UNREAD_TEXT:
            scan.unknown = True  # ``$c ~``, ``$(echo cd) ~``, ``. ./x.sh``, ``alias c='cd ~'``
        if argv[0] in _CD_BUILTINS:
            scan.found.append((argv[0], argv[1:]))
        if argv[0] in _DIR_OPTION_COMMANDS:
            scan.found += [("cd", [d]) for d in _dir_option_values(argv[0], argv[1:])]
        if raw and _dangling_dir_option(argv, raw[-1]) and any(os.path.basename(t) in _FEEDERS for t in raw):
            scan.unknown = True  # ``xargs tar -cf - -C``: the input is the directory
        scan.tar = scan.tar or argv[0] in _TAR_COMMANDS
        if argv[0] == "find" and _FIND_EXEC.intersection(argv):
            scan.replaces.append("{}")
        scan.replaces += _parallel_replaces(raw)
        if _makes_symlink(argv[0], argv[1:]):
            scan.links = True
            scan.link_names += _link_names(argv[1:])
        scan.links = scan.links or _unpacks(argv[0], argv[1:])
    for child in parsed.children:
        if child.clean in trap_payloads or any(s in trap_payloads for s in child.subs):
            continue  # a trap action runs later: its cd does not move the current line
        _cd_commands(child, scan)
    return scan


def _cd_operand(name: str, args: list[str]) -> str | None:
    """The directory ``cd``/``pushd`` is given: ``~`` for a bare ``cd``; None when the command stays in the
    directories it has (``pushd`` alone swaps, ``pushd +1`` rotates). ``pushd -n DIR`` does not change directory
    itself, but ``popd`` or a bare ``pushd`` comes back to DIR, so it counts as reached."""
    operands: list[str] = []
    options_over = False
    for a in args:
        if not options_over and a == "--":
            options_over = True
        elif not options_over and a.startswith(("-", "+")) and a != "-":
            if name == "pushd" and a[1:].isdigit():
                return None
        else:
            operands.append(a)
    if operands:
        return operands[0]
    return "~" if name == "cd" else None


def _cd_targets(operand: str, bases: tuple[str, ...], home: str, cdpath: bool) -> list[str] | None:
    """The directories ``cd OPERAND`` can land in from any of ``bases``, or None when that cannot be worked out: ``cd
    -`` (the directory before), an unresolved ``$VAR``/substitution, an unknown ``~user``, a glob, or a relative name
    while ``CDPATH`` may redirect it."""
    operand = operand.strip("'\"")
    if not operand:
        return list(bases)  # ``cd ""`` stays where it is
    if operand == "-":
        return None
    out: list[str] = []
    for word in _brace_expand(operand):
        if _GLOB_CHARS & set(word):
            return None
        if word.startswith("~") and word != "~" and not word.startswith("~/"):
            expanded = os.path.expanduser(word)
            if expanded == word:
                return None  # a user this machine does not know, ``~+``, ``~-``
            word = expanded
        if cdpath and word not in (".", "..") and not word.startswith(("/", "~", "./", "../", "$")):
            return None
        for base in bases:
            path = hardline.expand_vars(word, base, home)
            if "$" in path or "`" in path:
                return None
            out.append(os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path)))
    return out


def _where(parsed: hardline.Parsed, base: str | _Where, home: str) -> _Where:
    """The directories the relative paths on ``parsed`` may be relative to. A ``cd`` anywhere on the line counts for
    every command on it, whatever their order or nesting (``(cd ~; cat .ssh/id_rsa)``, ``bash -c 'cd ~ && ...'``, a
    loop that comes back round): the line is judged against its starting directory and each directory its ``cd``s can
    reach, each ``cd`` applied to every directory found before it (``cd ~ && cd .ssh``). One that cannot be resolved
    makes the directory unknown (``unknown``), which the credential check treats like an unknown variable."""
    start = base if isinstance(base, _Where) else _Where((base,))
    scan = _cd_commands(parsed)
    replaces = tuple(dict.fromkeys([*start.replaces, *scan.replaces]))
    links = tuple(dict.fromkeys([*start.links, *scan.link_names]))
    cds, cdpath = scan.found, scan.cdpath
    dirs = list(start.dirs)
    home_moved = start.home_moved or scan.home
    unknown = start.unknown or scan.unknown or (scan.tar_options and scan.tar)  # ``TAR_OPTIONS`` may hold a ``-C``
    # a link made on the line is not on disk yet when the line is judged, so a ``cd`` may go through it anywhere
    unknown = unknown or (scan.links and any(_cd_operand(name, args) is not None for name, args in cds))
    passes_allowed = _CD_PASSES if _loops(parsed) else 1
    for passes in range(passes_allowed):
        grew = False
        for name, args in cds:
            operand = _cd_operand(name, args)
            if operand is None:
                continue
            if any(r in operand for r in scan.replaces):
                unknown = True  # ``xargs -I{} sh -c 'cd {}'``: the directory is whatever the input names
                continue
            targets = _cd_targets(operand, tuple(dirs), home, cdpath)
            if targets is None:
                unknown = True
                continue
            for d in targets:
                if d not in dirs:
                    dirs.append(d)
                    grew = True
            if len(dirs) > _CD_MAX_DIRS:
                return _Where(tuple(dirs[:_CD_MAX_DIRS]), True, home_moved, replaces, links)
        if not grew:
            break
        if passes_allowed > 1 and passes == passes_allowed - 1:
            unknown = True  # still reaching new directories after the last replay
    return _Where(tuple(dirs), unknown, home_moved, replaces, links)


def _sub_secret_access(
    sub: str, piped: bool, where: _Where, home: str, budget: _GlobBudget | None = None
) -> str | None:
    plain = _HARMLESS_REDIRECT.sub("", sub)
    worst: str | None = None
    for source in _INPUT_REDIRECT.findall(plain):
        worst = _worse(worst, _classify_arg(source, where, home, budget))
    wrapper_reads: list[str] = []  # ``xargs -a .env echo`` prints the file whatever it wraps
    argv = _argv(sub, wrapper_reads)
    for arg in wrapper_reads:
        worst = _worse(worst, _classify_arg(arg, where, home, budget))
    if not argv:
        return worst
    for command in hardline.ssh_option_commands(argv[0], argv[1:]):
        # ``ssh -oProxyCommand='head .env' x``: ssh runs it, and its output goes on to ssh
        inner = hardline.parse(command)
        worst = _worse(worst, _secret_access(inner, _where(inner, where, home), home, consumed=True, budget=budget))
    reads, writes, confirm = _credential_roles(argv[0], argv[1:], piped)
    for arg in reads:
        worst = _worse(worst, _classify_arg(arg, where, home, budget))
    for args, verdict in ((writes, "ask"), (confirm, "confirm")):
        found = [_classify_arg(arg, where, home, budget) for arg in args]
        if "unchecked" in found:
            worst = _worse(worst, "unchecked_w")
        elif any(found):
            worst = _worse(worst, verdict)
    return worst


def _secret_access(
    parsed: hardline.Parsed, where: _Where, home: str, consumed: bool = False, budget: _GlobBudget | None = None
) -> str | None:
    """How a command line touches credential files: ``deny`` (reads, copies or sends one), ``human`` (a variable may
    point at one), ``unchecked`` (a glob was too large to check), ``confirm`` (deletes, sources, stages one or hands
    it to a container), ``ask`` (overwrites one), None. Every argument of every simple command and of everything
    nested in it counts unless the command only prints, tests or authenticates with it (see _credential_roles), and
    so do ``<`` sources; ``$HOME``, ``$PWD`` and ``~`` are expanded, globs are matched."""
    budget = budget or _GlobBudget()  # one for the whole command line, nested commands included
    worst: str | None = None
    # once per line, not per sub: a line of thousands of simple commands made this subs x pipelines (K3NOX-691)
    in_pipes = {sub for p in parsed.pipelines if len(p) > 1 for sub in p}
    for sub in parsed.subs:
        piped = consumed or sub in in_pipes
        worst = _worse(worst, _sub_secret_access(sub, piped, where, home, budget))
    # the first children are ``$(...)``/backtick/``<(...)`` payloads: their output becomes the outer arguments
    n_subst = sum(len(ps) for ps in parsed.sub_payloads)
    for k, child in enumerate(parsed.children):
        worst = _worse(worst, _secret_access(child, where, home, consumed or k < n_subst, budget))
    return worst


def _rule_forms(parsed: hardline.Parsed) -> list[str]:
    """What else the subs of ``parsed`` run as, for deny/ask rules: their normalised argv (``/bin/rm x`` is ``rm x``,
    ``git -C . push`` is ``git push``) and every nested command (``bash -c '...'``, ``$(...)``) in both spellings."""
    forms: list[str] = []
    for sub in parsed.subs:
        norm = " ".join(_argv(sub))
        if norm and norm != sub:
            forms.append(norm)
    for nested in parsed.nested:
        forms.append(nested)
        norm = " ".join(_argv(nested))
        if norm and norm != nested:
            forms.append(norm)
    return list(dict.fromkeys(forms))


_UNCHECKED_MSG = "The glob is too large to check for credential files; narrow it"


def _decide_bash(
    mode: PermissionMode, command: str, ruleset: list[Rule], extra: list[str] | None, roots: list[str], cwd: str
) -> Decision:
    home = os.path.expanduser("~")
    hit, parsed = hardline.check_parse(command, extra, cwd=cwd, home=home)
    if hit:
        return Decision(action="deny", message=f"Hardline deny ({hit}): {command[:120]}", hardline=hit)
    subs = parsed.subs
    # an open quote or a quote inside a comment: the shell may not run what was parsed, so no rule vouches for it
    unsafe = parsed.unterminated or parsed.comment_quote
    secret = _secret_access(parsed, _where(parsed, cwd, home), home)
    if secret == "deny":
        hit = "sensitive-path"
        return Decision(action="deny", message=f"Hardline deny ({hit}): {command[:120]}", hardline=hit)
    prefixes = [_rule_pattern_base(s) for s in subs]
    if mode == PermissionMode.PLAN:
        return Decision(action="deny", patterns=prefixes, message=PLAN_MSG)
    if mode == PermissionMode.YOLO:
        if secret == "unchecked":
            return Decision(action="deny", patterns=prefixes, message=f"{_UNCHECKED_MSG}: {command[:120]}")
        return Decision(action="allow", patterns=prefixes)
    if mode == PermissionMode.AUTO and any(_writes_outside_roots(sub, roots, cwd) for sub in subs):
        # auto mode writes only inside the project roots; no allow rule overrides this
        return Decision(
            action="deny", patterns=prefixes, message=f"Auto mode writes only inside the project roots: {command[:120]}"
        )
    worst: Rule | None = None
    home_dot = False
    sandbox_only = False
    for k, sub in enumerate(subs):
        rule = evaluate("bash", sub, ruleset, default="ask")
        if rule.layer == 0 and rule.pattern in _SANDBOXED:
            # a user/project/session rule that names it (`make *: allow|ask`) decides: an allow is the user's own (not
            # sandbox-bound), an ask is kept; only a bare `*` catch-all is less specific than this allowlist
            mine = evaluate("bash", sub, [r for r in ruleset if r.layer > 0], default="ask")
            rule = mine if mine.layer > 0 and mine.pattern != "*" else rule
        if rule.action == "allow" and rule.layer == 0 and _home_dot_path(sub, roots, cwd):
            home_dot = True  # a person answers it, in auto mode too (see below)
            rule = Rule(tool="bash", pattern="*", action="ask")
        sandbox_only = sandbox_only or (rule.layer == 0 and rule.action == "allow" and rule.pattern in _SANDBOXED)
        # no rule vouches for a program fed to an interpreter through a heredoc (`bash <<EOF`, `cat <<EOF | sh`): the
        # command line does not say what runs
        if rule.action == "allow" and (
            parsed.code_heredoc[k] or _voids_allow(sub, rule, ruleset, roots, cwd, parsed.heredocs[k])
        ):
            rule = Rule(tool="bash", pattern="*", action="ask")
        if rule.action == "allow" and rule.layer == 0 and "\n" in sub:
            unsafe = True  # the builtin read-only allowlist covers one-line commands only
        if worst is None or _RANK[rule.action] > _RANK[worst.action]:
            worst = rule
    # deny/ask rules also see through wrappers, absolute paths and nested commands (`/bin/rm x`, `bash -c 'rm x'`);
    # an allow is only ever granted from the command as written, and a bare "*" catch-all would make every form ask
    strict = [r for r in ruleset if r.pattern != "*"]
    for form in _rule_forms(parsed):
        rule = evaluate("bash", form, strict, default="allow")
        if rule.action != "allow" and (worst is None or _RANK[rule.action] > _RANK[worst.action]):
            worst = rule
    worst = worst or Rule(tool="bash", pattern="*", action="ask")
    if unsafe and worst.action != "deny":
        return Decision(
            action="ask",
            patterns=prefixes,
            needs_human=True,
            message=f"Command could not be parsed safely; confirm it yourself: {command[:120]}",
        )
    if secret in ("human", "confirm", "unchecked", "unchecked_w") and worst.action != "deny":
        why = {
            "human": "A variable may point at a credential file",
            "confirm": "Deletes, sources, stages or hands a container a credential file",
            "unchecked": _UNCHECKED_MSG,
            "unchecked_w": _UNCHECKED_MSG,
        }[secret]
        return Decision(
            action="ask", patterns=prefixes, needs_human=True, message=f"{why}; confirm it yourself: {command[:120]}"
        )
    if home_dot and worst.action != "deny":
        return Decision(
            action="ask",
            patterns=prefixes,
            needs_human=True,
            message=f"Reads a dot folder of the home directory, where credentials live; confirm it yourself: "
            f"{command[:120]}",
        )
    if secret == "ask" and worst.action == "allow":
        return Decision(action="ask", patterns=prefixes, message=f"Overwrites a credential file: {command[:120]}")
    if worst.action == "allow" and not _inside(cwd, roots):
        return Decision(action="ask", patterns=prefixes, message=f"Working directory outside project roots: {cwd}")
    return Decision(
        action=worst.action, patterns=prefixes, rule=worst, sandbox_only=sandbox_only and worst.action == "allow"
    )


def _decide_path(
    mode: PermissionMode,
    tool: str,
    path: str,
    roots: list[str],
    ruleset: list[Rule],
    outside_reads: PermissionMode | None = None,
) -> Decision:
    """``outside_reads``: the mode whose policy decides a read outside the roots that no user/project/session rule
    decides (default ``mode``; auto for the planning turn of an auto session, see decide)."""
    is_edit = tool in EDIT_TOOLS
    outside_reads = outside_reads or mode
    if is_edit and mode == PermissionMode.PLAN:
        return Decision(action="deny", patterns=[path], message=PLAN_MSG)
    if not is_edit and sensitive_path(path):
        # read/grep/glob run in the daemon process, outside the sandbox: they would hand the keys to the model
        return Decision(
            action="deny", patterns=[path], hardline="sensitive-path", message=f"Hardline deny (sensitive-path): {path}"
        )
    inside = _inside(path, roots)
    if not inside:
        fallback: Action = "ask"
    elif is_edit:
        fallback = "allow" if mode in (PermissionMode.ACCEPT_EDITS, PermissionMode.AUTO, PermissionMode.YOLO) else "ask"
    else:
        fallback = "allow"
    rule = evaluate("edit" if is_edit else "read", path, ruleset, default=fallback)
    dec = Decision(action=rule.action, patterns=[path], rule=rule)
    if not inside:
        uv_credentials = _inside(path, [os.path.join(os.path.expanduser("~"), _UV_CREDENTIALS)])
        if rule.layer == 0:
            dec.message = f"Outside project roots: {path}"
        if is_edit and mode == PermissionMode.AUTO and rule.action == "ask":
            # auto mode writes only inside the project roots; an explicit user allow rule still applies
            dec.action, dec.message = "deny", f"Auto mode writes only inside the project roots: {path}"
        elif not is_edit and rule.action == "ask" and rule.layer == 0 and uv_credentials:
            # uv's credential store: a person approves the read in every mode, auto included (see _UV_CREDENTIALS)
            dec.needs_human = True
        elif not is_edit and rule.action == "ask" and rule.layer == 0 and _toolchain_read(path):
            # a read of the toolchain dirs (/usr, site-packages, node_modules ...) is never worth a prompt: every
            # mode allows it (auto logs it as an auto allow, see _finish, and alone takes /etc too). A
            # user/project/session rule still wins; credential paths were refused above.
            if outside_reads != PermissionMode.AUTO and not _inside(path, ["/etc"]):
                dec.action = "allow"
            elif outside_reads != PermissionMode.AUTO:
                dec.needs_human = True  # /etc outside auto: a person approves it, as before
            elif mode != PermissionMode.AUTO:
                dec.action, dec.auto_allowed = "allow", True  # what _finish does for auto itself
        elif not is_edit and rule.action == "ask" and outside_reads == PermissionMode.AUTO and rule.layer == 0:
            # auto mode does not stop for a wandering read: anything outside the toolchain is refused with a hint
            dec.action = "deny"
            dec.message = f"{path} is outside the project: stay inside the project, or ask the user to /add-dir it"
        elif not is_edit and rule.action == "ask":
            # an in-process read outside the roots is never auto-allowed: a person approves it (or adds a read rule)
            dec.needs_human = True
    if not is_edit and rule.layer == 0 and dec.action != "deny":
        # a root that covers a credential store of $HOME, or $HOME itself, makes these in-roots reads: they run in the
        # daemon, where the sandbox that hides them from bash does not apply. A person answers, in every mode (yolo
        # and auto included); a user/project/session rule still decides.
        why = None
        if _home_store_read(path, roots):
            why = "Reads a credential store of the home directory"
        elif _home_dot_read(path, roots) and not _toolchain_read(path):
            why = "Reads a dot folder of the home directory, where credentials live"
        if why:
            dec.action, dec.needs_human, dec.always_ask = "ask", True, True
            dec.message = f"{why}; confirm it yourself: {path}"
    if is_edit and dec.action != "deny" and secret_store(path):
        # write/edit run in the daemon, outside the sandbox: ~/.ssh/authorized_keys or a file in a Windows profile's
        # AppData (its Startup folder) is changed only when a person says so; yolo still allows it (issue #134)
        dec.action, dec.needs_human = "ask", True
        dec.message = f"Writes into a key or credential store; confirm it yourself: {path}"
    return dec


#: Read-only system and toolchain locations an auto-mode read outside the roots may look at without a person:
#: ``/usr`` and ``/etc`` (the sandbox shows bash those anyway), the system package trees ``/opt`` and ``/nix/store``
#: (the sandbox does not bind them: only these in-process reads see them), and the language package caches under
#: ``$HOME``. Credential paths are refused before this is consulted (sensitive_path). uv counts only with its
#: interpreters and tools: ``~/.local/share/uv/credentials`` (``uv auth dir``) holds index tokens.
_TOOLCHAIN_SYSTEM = ("/usr", "/opt", "/etc", "/nix/store")
_TOOLCHAIN_HOME = (
    ".cargo/registry",
    ".cargo/git",
    ".rustup",
    "go/pkg/mod",
    ".npm",
    ".local/share/uv/python",
    ".local/share/uv/tools",
    ".m2/repository",
)
#: uv's credential store (``uv auth dir``): an auto-mode read there asks a person, as every out-of-roots read does
#: in the other modes, instead of being refused like any other path outside the toolchain dirs.
_UV_CREDENTIALS = ".local/share/uv/credentials"
#: Path segments of installed packages of a known toolchain (Python, Node).
_TOOLCHAIN_SEGMENTS = ("site-packages", "dist-packages", "node_modules")


def _toolchain_read(path: str) -> bool:
    """``path`` (absolute) lies in a read-only system or toolchain location (see _TOOLCHAIN_SYSTEM), after resolving
    symlinks: a link from there into ``$HOME`` does not count."""
    home = os.path.expanduser("~")
    if _inside(path, [*_TOOLCHAIN_SYSTEM, *(os.path.join(home, rel) for rel in _TOOLCHAIN_HOME)]):
        return True
    return any(seg in _TOOLCHAIN_SEGMENTS for seg in _real(path).split(os.sep))


@functools.lru_cache(maxsize=32)
def _home_stores_cached(home: str, home_real: str, hidden: tuple[str, ...]) -> tuple[str, ...]:
    """The materialized credential-store list for one $HOME (see _home_stores); cached, 32 most recent homes."""
    stores = [
        os.path.join(h, rel)
        for h in dict.fromkeys((os.path.normpath(home), home_real))
        for rel in (*hidden, _UV_CREDENTIALS)
    ]
    return tuple(dict.fromkeys([*stores, *map(_real, stores)]))


def _home_stores() -> list[str]:
    """The credential stores of ``$HOME`` that the read-only home sandbox hides from bash (sandbox.HOME_RO_HIDDEN:
    ``~/.netrc``, ``~/.npmrc``, ``~/.config/gh``, ``~/.docker`` ...) plus uv's (_UV_CREDENTIALS): each below ``$HOME``
    as written and below its realpath, and each one's own realpath (a dotfile manager's symlink). Cached on
    ($HOME, its realpath, the hidden list): $HOME only changes at process start or in tests, so a hot-path read
    decide pays one realpath instead of ~65."""
    from k3code.reliability.sandbox import HOME_RO_HIDDEN  # that module imports this one

    home = os.path.expanduser("~")
    return list(_home_stores_cached(home, _real(home), tuple(HOME_RO_HIDDEN)))


def _flat(path: str) -> str:
    """``path`` (absolute) normalised, with a leading ``//`` made ``/``: normpath keeps two slashes (POSIX leaves them
    implementation-defined) and the kernel does not: ``//home/u/.npmrc`` is ``/home/u/.npmrc`` and must compare so."""
    return "/" + os.path.normpath(path).lstrip("/")


def _spellings(path: str) -> list[str]:
    """``path`` (absolute) as written, normalised, and its realpath."""
    return list(dict.fromkeys((_flat(path), _real(path))))


def _stores_holding(spellings: list[str], stores: list[str]) -> list[str]:
    """The entries of ``stores`` that a path lies in, by any of its ``spellings``. Compared case-insensitively, as
    secret_store does: on drvfs and macOS ``~/.NPMRC`` is ``~/.npmrc``."""
    folded = [p.casefold() for p in spellings]
    out: list[str] = []
    for store in stores:
        low = store.casefold().rstrip("/")
        if any(p == low or p.startswith(low + "/") for p in folded):
            out.append(store)
    return out


def _home_store_read(path: str, roots: list[str]) -> bool:
    """``path`` lies in a credential store of ``$HOME`` (see _home_stores) that a root covers whole: a root at
    ``$HOME`` or above it, or one such as ``~/.config`` or ``~/.local``. A root inside the store makes its own files
    ordinary (a session started in ``~/.claude/skills/x`` reads that skill)."""
    for store in _stores_holding(_spellings(path), _home_stores()):
        if _inside(store, roots) and not any(_inside(path, [r]) and _inside(r, [store]) for r in roots):
            return True
    return False


@functools.lru_cache(maxsize=32)
def _home_prefixes_cached(home: str, home_real: str) -> tuple[str, ...]:
    """``$HOME/`` spellings for one home (see _home_prefixes); cached like _home_stores_cached."""
    return tuple(h.rstrip("/") + "/" for h in dict.fromkeys((home, home_real)))


def _home_prefixes() -> list[str]:
    """``$HOME/`` as written and by its realpath."""
    home = os.path.normpath(os.path.expanduser("~"))
    return list(_home_prefixes_cached(home, _real(home)))


def _below_home_dot(path: str, homes: list[str]) -> bool:
    """``path`` (absolute, normalised) has a component below one of ``homes`` (see _home_prefixes) that starts with a
    dot: ``~/.netrc``, ``~/.config/gh/hosts.yml``, ``~/src/x/.github/ci.yml``."""
    return any(path.startswith(h) and any(c.startswith(".") for c in path[len(h) :].split("/")) for h in homes)


def _home_dot_read(path: str, roots: list[str]) -> bool:
    """What _home_dot_path finds on a bash command line, for the in-process readers: ``path`` lies below a dot folder
    of ``$HOME`` (or is a dotfile there), a root at or above ``$HOME`` covers it and no project root below ``$HOME``
    does (``.github`` there is ordinary). Credential stores sit in dot folders no list names in full
    (``~/.config/git/credentials``, ``~/.cache/huggingface/token``). Checked on the path as written and its realpath."""
    return _home_dot_spelled(_spellings(path), roots)


def _home_dot_spelled(spellings: list[str], roots: list[str]) -> bool:
    """_home_dot_read for a path given as its spellings."""
    home = os.path.normpath(os.path.expanduser("~"))
    if not _inside(home, roots):
        return False
    projects = [r for r in roots if not _inside(home, [r])]
    homes = _home_prefixes()
    return any(_below_home_dot(p, homes) and not (projects and _inside(p, projects)) for p in spellings)


def _link_hops(path: str) -> list[str]:
    """``path`` (absolute) as written and, for each symlink met on the way to its realpath, what the path becomes
    after following it: ``~/.config/gh/hosts.yml -> ~/dotfiles/gh/hosts.yml`` is both. A reader that opens the final
    target would otherwise never see that the path it was given names a credential store."""
    hops: list[str] = []
    cur = path
    try:
        for _ in range(_MAX_LINK_HOPS):
            hops.append(_flat(cur))
            done: list[str] = []
            rest = cur.split("/")
            for i, comp in enumerate(rest):
                if comp in ("", "."):
                    continue
                if comp == "..":
                    if done:
                        done.pop()
                    continue
                probe = "/" + "/".join([*done, comp])
                if os.path.islink(probe):
                    cur = os.path.join(os.path.join("/" + "/".join(done), os.readlink(probe)), *rest[i + 1 :])
                    break
                done.append(comp)
            else:
                break
    except (OSError, ValueError):
        pass
    return list(dict.fromkeys([*hops, _real(path)]))


_MAX_LINK_HOPS = 40  # as the kernel's symlink limit


def _under(path: str, base: str, fold: bool = False) -> bool:
    """``path`` is ``base`` or lies below it, by name (not resolved again). ``fold`` compares case-insensitively, as
    secret_store does: only the test that a path lies in a credential store wants that (on drvfs and macOS
    ``~/.NPMRC`` is ``~/.npmrc``). A root that covers a path, or lies in a store, does so by its exact spelling."""
    low, root = (path.casefold(), base.casefold().rstrip("/")) if fold else (path, base.rstrip("/"))
    return low == root or low.startswith(root + "/")


def _mirrored(path: str, homes: list[str], projects: list[str]) -> bool:
    """``path`` lies in a copy of a project root that keeps the root's place below ``$HOME``, in a folder of its own
    without a dot: a GNU stow package (``~/dotfiles/k3code/.k3code/memory/USER.md`` for the root ``~/.k3code/memory``).
    Only a root whose own place has a dot component needs this: the copy is the same files, so its dot folders are as
    ordinary as the root's; any other dot folder of the package (``.config/gh``) is not."""
    for home in homes:
        if not path.startswith(home):
            continue
        parts = path[len(home) :].split("/")
        for root in projects:
            rel = next((root[len(h) :].split("/") for h in homes if root.startswith(h)), [])
            if not any(c.startswith(".") for c in rel):
                continue
            for i in range(1, len(parts) - len(rel) + 1):
                if any(c.startswith(".") for c in parts[:i]):
                    break
                if parts[i : i + len(rel)] == rel:
                    return True
    return False


def memory_import_withheld(path: str, roots: Sequence[str], extra_stores: Sequence[str] = ()) -> bool:
    """``path`` (absolute, as written: not resolved) must not be pulled into the prompt by the memory loader, which
    has no ``decide()`` of its own: a key or dotenv file (sensitive_path); a file in a credential store of ``$HOME``
    (see _home_stores) or in one of ``extra_stores``; or, with a root at or above ``$HOME``, anything below a dot
    folder of ``$HOME``. Every spelling is checked: the path as written, each hop of a symlink chain and the realpath
    (see _link_hops), so a store file that links out and a plain file that links in are both caught.

    The roots decide what is ordinary, as for the in-process readers (_home_store_read, _home_dot_read): a root inside
    a store, with the whole path inside that root, makes its own files ordinary (a session started in
    ``~/.claude/skills/x`` reads that skill's ``CLAUDE.md``), and a root below ``$HOME`` that does not contain it
    makes its dot folders ordinary (``.github`` of a repository). A root counts as written and by its realpath, and a
    spelling lies in it by name: a spelling is never resolved a second time, or a memory folder kept in a dotfiles
    repository (``~/.k3code/memory -> ~/dotfiles/k3code/memory``) would stop being its own. Callers have already
    checked the target lies in the roots, so a store outside every root needs no test here."""
    spellings = _link_hops(path)
    if any(sensitive_path(p) for p in spellings):
        return True
    forms = {r: _spellings(r) for r in map(str, roots)}  # each root as written and by realpath

    def covers(root: str, paths: list[str]) -> bool:
        return all(any(_under(p, f) for f in forms[root]) for p in paths)

    for store in [*_home_stores(), *extra_stores]:
        held = [p for p in spellings if _under(p, store, fold=True)]
        if held and not any(any(_under(f, store) for f in forms[r]) and covers(r, held) for r in forms):
            return True
    home = os.path.normpath(os.path.expanduser("~"))
    if not _inside(home, list(forms)):
        return False
    projects = [f for r in forms if not _inside(home, [r]) for f in forms[r]]
    homes = _home_prefixes()
    return any(
        _below_home_dot(p, homes) and not (any(_under(p, f) for f in projects) or _mirrored(p, homes, projects))
        for p in spellings
    )


def search_withheld(searched: str) -> Callable[[str], bool]:
    """Which files a search below ``searched`` (grep over a directory) returns no lines of: a credential file
    (sensitive_path); a file in a credential store of ``$HOME`` (see _home_stores) that ``searched`` is not itself
    inside (a search of the store was decided on that path); and, in a search of ``$HOME`` or a directory above it,
    every file below a dot folder of ``$HOME``, whose read asks a person there (see _decide_path). A walk of ``$HOME``
    thus leaves a project's ``.github`` out too: search the project."""
    inside = _spellings(searched)
    # _stores_holding per file, precomputed: a walk of $HOME checks every file it finds
    stores = [s.casefold().rstrip("/") for s in _home_stores() if not _stores_holding(inside, [s])]
    exact, below = frozenset(stores), tuple(s + "/" for s in stores)
    homes = _home_prefixes() if _inside(os.path.expanduser("~"), [searched]) else []

    def withheld(path: str) -> bool:
        if sensitive_path(path):
            return True
        for p in _spellings(path):
            low = p.casefold()
            if low in exact or low.startswith(below) or _below_home_dot(p, homes):
                return True
        return False

    return withheld


#: ``$HOME`` entries that hold keys or credentials (the sandbox masks the first two, see sandbox.HOME_SECRETS).
_HOME_SECRETS = (".config/k3code", ".ssh", ".gnupg", ".aws")
_PROC_ENVIRON = re.compile(r"^/proc/[^/]+/(?:task/[^/]+/)?environ$")
_DOTENV_TEMPLATES = (".example", ".sample", ".template", ".dist")


def _secret_dirs() -> list[str]:
    home = os.path.expanduser("~")
    dirs = [os.path.join(home, s) for s in _HOME_SECRETS]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        dirs.append(os.path.join(xdg, "k3code"))
    return dirs


#: Entries of a Windows profile (``<drive>/Users/<name>``) that hold keys or credentials: the ssh/cloud/gpg folders,
#: AppData (browser credential stores, the Startup folder) and the registry hive files ``NTUSER.DAT*``. Lower case.
_WINDOWS_PROFILE_SECRETS = (".ssh", ".aws", ".gnupg", "appdata")


def windows_drives(mounts: str | None = None) -> list[str]:
    """Mount points (lower case) of the Windows drives WSL mounts (drvfs; ``9p`` with ``aname=drvfs`` on WSL 2), read
    from ``/proc/mounts`` or ``mounts``. Empty elsewhere."""
    if mounts is None:
        return list(_mounted_windows_drives())
    out: list[str] = []
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 4 and (fields[2] == "drvfs" or (fields[2] == "9p" and "aname=drvfs" in fields[3])):
            out.append(fields[1].replace("\\040", " ").rstrip("/").lower() or "/")
    return out


@functools.cache
def _mounted_windows_drives() -> tuple[str, ...]:
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            return tuple(windows_drives(f.read()))
    except OSError:
        return ()


def windows_profile_secret(path: str, drives: list[str] | None = None) -> bool:
    """``path`` (absolute) is a credential location in a Windows profile that WSL mounts: ``/mnt/c/Users/<name>/.ssh``,
    ``.aws``, ``.gnupg``, ``AppData`` or ``NTUSER.DAT*`` (case-insensitive, as drvfs is). A session started in
    ``C:\\Users\\<name>`` has all of them inside its project root (issue #134)."""
    folded = path.lower()
    for drive in windows_drives() if drives is None else drives:
        prefix = drive.rstrip("/") + "/users/"
        if folded.startswith(prefix):
            parts = folded[len(prefix) :].split("/")
            if len(parts) >= 2 and (parts[1] in _WINDOWS_PROFILE_SECRETS or parts[1].startswith("ntuser.dat")):
                return True
    return False


def sensitive_path(path: str, dirs: list[str] | None = None) -> bool:
    """``path`` (absolute) is a key/credential location: ``~/.ssh``, ``~/.gnupg``, ``~/.aws``, the k3code secrets dir
    (``~/.config/k3code``, ``$XDG_CONFIG_HOME/k3code``: the daemon's ``env`` file), a process environment
    (``/proc/*/environ``), a dotenv file, or the keys and credential stores of a Windows profile WSL mounts (see
    :func:`windows_profile_secret`). Checked on the path as written and on its realpath (symlinks), and
    case-insensitively: on drvfs (``/mnt/c``) and macOS ``.ENV`` and ``~/.SSH/id_rsa`` open the same files. ``dirs``
    is :func:`_folded_secret_dirs`, for a caller that checks many paths."""
    if secret_store(path, dirs):
        return True
    return any(_dotenv_name(os.path.basename(p)) for p in dict.fromkeys((os.path.normpath(path), _real(path))))


def secret_store(path: str, dirs: list[str] | None = None) -> bool:
    """``path`` (absolute) is in a key or credential store (``sensitive_path`` without the dotenv files): the secret
    dirs of ``$HOME``, a process environment, a Windows profile's keys and AppData. Path as written and realpath,
    compared case-insensitively: on drvfs (``/mnt/c``) and macOS ``~/.SSH`` is ``~/.ssh`` (issue #133)."""
    folded_dirs = dirs if dirs is not None else _folded_secret_dirs()
    for p in dict.fromkeys((os.path.normpath(path), _real(path))):
        folded = p.lower()
        if _PROC_ENVIRON.match(p) or any(folded == d or folded.startswith(d.rstrip("/") + "/") for d in folded_dirs):
            return True
        if windows_profile_secret(p):
            return True
    return False


def _folded_secret_dirs() -> list[str]:
    """The secret dirs as written and as realpaths, lower case."""
    dirs = [os.path.normpath(d) for d in _secret_dirs()]
    return [d.lower() for d in dict.fromkeys(dirs + [_real(d) for d in dirs])]


def _glob_base(path: str, pattern: str) -> str:
    """The directory a glob of ``pattern`` under ``path`` starts from: its literal leading segments count, so a
    pattern such as ``../../.ssh/*`` is decided as ``~/.ssh``."""
    if os.path.isabs(pattern):
        return os.path.normpath(pattern)  # pathlib refuses it; decide the absolute prefix anyway
    literal: list[str] = []
    for seg in pattern.split("/"):
        if any(ch in seg for ch in "*?["):
            break
        literal.append(seg)
    return os.path.normpath(os.path.join(path, *literal)) if literal else path


def _drop_assignments(prefix: str) -> str:
    """``prefix`` without its leading ``NAME=value`` words (environment assignments, often secrets)."""
    words = prefix.split(" ")
    while words and "=" in words[0] and words[0].split("=", 1)[0].isidentifier():
        words.pop(0)
    return " ".join(words)


#: An exact command proposed as a rule must not carry wildcards, variables, substitutions or redirections.
_INEXACT = re.compile(r"[*?$`;&|<>\n]")


def _rule_pattern_base(sub: str) -> str:
    """What a bash decision records for ``sub``: its arity prefix, or the exact command for the commands whose
    arguments name what runs (hardline.WILDCARD_UNSAFE: ``uv run pytest``, ``docker run --rm alpine``).

    The prefix drops flags but keeps their values (``git -C sub commit`` -> ``git sub``, ``git --no-pager log`` ->
    ``git log``), so it is used only when the command starts with it as written; otherwise ``<prefix> *`` would never
    match the approved command (it asked again every time) or would name another one, and the exact command is
    recorded instead (#170)."""
    if hardline.wildcard_unsafe(sub):
        return sub
    prefix = hardline.command_prefix(sub)
    return prefix if prefix and _starts_with(sub, prefix) else sub


def _starts_with(sub: str, prefix: str) -> bool:
    """True when the words of ``sub`` (leading ``NAME=value`` assignments dropped) begin with exactly ``prefix``."""
    try:
        words = shlex.split(sub, posix=True)
    except ValueError:
        words = sub.split()
    while words and "=" in words[0] and words[0].split("=", 1)[0].isidentifier():
        words.pop(0)
    return any(" ".join(words[:n]) == prefix for n in range(1, len(words) + 1))


def exact_rule_pattern(command: str) -> str | None:
    """``command`` as an exact rule pattern (leading assignments dropped), or None when it cannot be one."""
    command = _drop_assignments(command.strip())
    return command if command and not _INEXACT.search(command) else None


def suggest_rules(tool: str, dec: Decision) -> list[Rule]:
    """Narrowest rules to persist for an ``always``/``session`` approval."""
    if tool == "bash":
        # "<prefix> *" matches "git commit" and "git commit -m x" but not "git commit-tree"/"shutdown".
        # Never for launchers (shells, ssh, interpreters, xargs, find, sudo ...): "always allow `python3 *`" would
        # allow every command the user will ever be asked about.
        # For the commands whose arguments name what runs (uv, npx, make, docker run ...) only the exact command.
        # A leading VAR=value is dropped: "always" on `API_KEY=sk-... curl x` wrote the key into config.yaml.
        out: list[Rule] = []
        for p in dict.fromkeys(_drop_assignments(p) for p in dec.patterns):
            if not p or hardline.is_launcher(p):
                continue
            # a recorded pattern that is not its own arity prefix is an exact command (see _rule_pattern_base)
            exact = hardline.wildcard_unsafe(p) or hardline.command_prefix(p) != p
            pattern = exact_rule_pattern(p) if exact else f"{p} *"
            if pattern:
                out.append(Rule(tool="bash", pattern=pattern, action="allow"))
        return out
    name = "edit" if tool in EDIT_TOOLS else "read" if tool in READ_TOOLS else tool
    return [Rule(tool=name, pattern=p, action="allow") for p in dec.patterns]
