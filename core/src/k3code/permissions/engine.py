"""Modes plus the central decide() entry point.

Modes: ``default`` (ask edits + non-allowlisted bash), ``accept-edits``
(edits allowed, bash asks), ``plan`` (read-only + ``exit_plan``), ``auto``
(everything not denied allowed, side effects logged), ``yolo`` (everything
except hardline; only the asks marked ``always_ask`` survive it: an edit of a protected file, see :func:`protected_path`
(a git hook or config, ``.envrc``, a shell startup file, a key ...), and a force push without a branch).
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

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
            f"{key} {value!r} is not one of: ask, auto-edit, plan, auto, yolo "
            f"(set in config.yaml or K3CODE_{key.upper()})"
        ) from None


#: Builtin read-only bash allowlist: safe to run without asking.
BUILTIN_BASH_ALLOW = [
    "ls *",
    "cat *",
    "grep *",
    "rg *",
    "git status *",
    "git diff *",
    "git log *",
]

#: ``rg --pre CMD`` runs CMD on every file; ``git diff|log --output=FILE`` writes FILE; ``--ext-diff``/``--textconv``
#: run configured external programs.
BUILTIN_BASH_ASK = [
    "rg* --pre*",
    "rg* --hostname-bin*",
    "git diff* --output*",
    "git log* --output*",
    "git diff* --ext-diff*",
    "git diff* --textconv*",
    "git log* --ext-diff*",
    "git log* --textconv*",
]

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
        "recall",
        "session_read",
    }
)
EDIT_TOOLS = frozenset({"write", "edit"})
READ_TOOLS = frozenset({"read", "grep", "glob"})  # path-taking read-only tools
EXIT_PLAN_TOOL = "exit_plan"
PLAN_HINT = "explore with read, glob and grep, then call exit_plan."
#: Both plan-mode messages start with "Plan mode is read-only": toolerrors.py counts a refusal that starts so as a
#: policy denial, not as an error of the project.
PLAN_MSG = f"Plan mode is read-only; bash/write/edit are unavailable: {PLAN_HINT}"


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
    #: an ask that no mode and no rule converts, yolo included (an edit of a protected file, see protected_path; a force
    #: push without a branch); headless it is a deny
    always_ask: bool = False


def builtin_defaults() -> list[Rule]:
    """Builtin ruleset: pure tools + safe bash allow, writes/other bash ask."""
    rules: list[Rule] = [Rule(tool=t, pattern="*", action="allow") for t in sorted(PURE_TOOLS - READ_TOOLS)]
    rules += [Rule(tool="bash", pattern=p, action="allow") for p in BUILTIN_BASH_ALLOW]
    # Flags that make an allowed read-only command execute or write something: longer patterns win, so these ask.
    rules += [Rule(tool="bash", pattern=p, action="ask") for p in BUILTIN_BASH_ASK]
    rules.append(Rule(tool=EXIT_PLAN_TOOL, pattern="*", action="allow"))
    return rules


_RANK: dict[str, int] = {"allow": 1, "ask": 2, "deny": 3}


def _norm(path: str | Path) -> str:
    return os.path.normpath(os.path.expanduser(str(path)))


def _abs(raw: str, cwd: str) -> str:
    p = os.path.expanduser(raw or ".")
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))


def _inside(path: str, roots: list[str]) -> bool:
    """``path`` lies under one of ``roots``, after resolving symlinks on both sides: a lexical check let
    ``proj/docs -> ../outside`` turn a write to ``docs/x`` into a write outside the project. A path that does not
    exist yet resolves through its nearest existing parent (realpath keeps the missing remainder as is)."""
    real = os.path.realpath(path)
    real_roots = [os.path.realpath(r) for r in roots]
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in real_roots)


def inside_roots(path: str | Path, cwd: str | Path, add_dirs: list[str] | None = None) -> bool:
    """``path`` (absolute) lies in the project: under the session cwd or one of its ``add_dirs``, the roots ``decide``
    judges "inside" by (symlinks resolved on both sides)."""
    roots = [_norm(cwd), *[_norm(d) for d in (add_dirs or [])]]
    # also the path as the kernel reads it: `link/../x` is `<target of link>/../x`, which `_norm` (lexical) collapsed
    # to a file inside the project while the file reached lies outside it (`decide` takes the stricter of the two)
    return _inside(_norm(path), roots) and _inside(str(path), roots)


#: Files the edit tools never change without a person, in any mode and whatever the rules say: git runs them (hooks, a
#: ``filter`` or ``diff`` driver or ``core.fsmonitor`` in a git config, ``.gitattributes`` choosing the driver, the
#: ``.git`` FILE of a linked worktree naming its git directory), direnv runs ``.envrc`` on the next ``cd``, husky its
#: hooks on the next commit, and ``.k3code/config*`` sets permission rules and hooks. ``write`` and ``edit`` run in the
#: daemon, outside the sandbox, and the harness's own git runs there too (scope classification, worktrees).
#: Matched at any depth and case-insensitively; ``.git`` stands for everything under it.
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


def protected_path(path: str) -> bool:
    """``path`` (absolute) is a file the edit tools never change without a person (see _PROTECTED), a file that runs as
    code or holds credentials wherever it sits (see _CODE_LOCATION: a nested repository's hooks, ``.env``, ``.npmrc``,
    a project's ``.ssh`` ...), a shell startup file or git config in the home directory, or under a credential
    directory (``~/.ssh``, ``~/.config/k3code``). Checked on the path as written and on its realpath, so a symlink does
    not hide the target."""
    home = os.path.expanduser("~")
    homes = (os.path.normpath(home), os.path.realpath(home))
    guarded = [os.path.join(h, rel) for h in homes for rel in _HOME_PROTECTED]
    secrets = _secret_dirs()
    guarded += [*map(os.path.normpath, secrets), *map(os.path.realpath, secrets)]
    guarded = [g.casefold().rstrip(os.sep) for g in guarded]
    for p in dict.fromkeys((os.path.normpath(path), os.path.realpath(path))):
        posix = p.replace("\\", "/")
        if _PROTECTED.search(posix) or _CODE_LOCATION.search(posix):
            return True
        low = p.casefold()
        if any(low == g or low.startswith(g + os.sep) for g in guarded):
            return True
    return False


def _stricter(a: Decision, b: Decision) -> Decision:
    return max((a, b), key=lambda d: (_RANK[d.action], d.needs_human))


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
) -> Decision:
    """Decide allow/ask/deny for one tool call.

    ``cwd`` is the session working dir (relative paths resolve against it);
    ``add_dirs`` extend the readable/writable roots. Order: hardline deny,
    then mode (plan/yolo), then rules (builtin < user < project < session),
    then auto-mode conversion of ask, then headless conversion of ask.
    """
    mode = PermissionMode(mode)
    args = args or {}
    cwd_s = _norm(cwd)
    roots = [cwd_s, *[_norm(d) for d in (add_dirs or [])]]
    ruleset = merge(builtin_defaults(), user_rules or [], project_rules or [], session_rules or [])

    if tool == "bash":
        # The command runs in args["cwd"] (relative to the session cwd): redirects resolve against it.
        bash_cwd = _abs(str(args.get("cwd") or "."), cwd_s)
        dec = _decide_bash(mode, str(args.get("command", "")), ruleset, hardline_extra, roots, bash_cwd)
    elif tool in EDIT_TOOLS or tool in READ_TOOLS:
        raw = os.path.expanduser(str(args.get("path") or args.get("file") or "."))
        path = _abs(raw, cwd_s)
        if tool == "glob":
            path = _glob_base(path, str(args.get("pattern") or ""))
        dec = _decide_path(mode, tool, path, roots, ruleset)
        # The tools and the kernel follow a symlink before they apply a `..` (`link/../x` is `<target of link>/../x`),
        # `_abs` applied it first: decide the file that is really reached as well, and take the stricter answer.
        reached = os.path.realpath(os.path.join(cwd_s, raw))
        if tool != "glob" and reached != os.path.realpath(path):
            dec = _stricter(dec, _decide_path(mode, tool, reached, roots, ruleset))
        if (
            tool in EDIT_TOOLS
            and dec.action != "deny"
            and (protected_path(path) or protected_path(reached) or sensitive_path(path) or sensitive_path(reached))
        ):
            # the edit tools run in the daemon, outside the sandbox: only a deny rule beats this, no allow rule, mode or
            # hook approval does (yolo and auto included; headless, an unattended run and a sub-agent it is a deny).
            # A file the model may not read (`sensitive_path`: .env.local, ~/.ssh, ...) is changed only on a person's
            # say: the read-first rule does not apply to it, so the approved diff is the only check there is
            dec.action, dec.needs_human, dec.always_ask = "ask", True, True
            dec.message = f"Edits a file that runs as code or holds credentials; confirm it yourself: {path}"
    elif tool == EXIT_PLAN_TOOL:
        dec = Decision(action="allow", patterns=["*"])
    else:
        base: Action = "deny" if mode == PermissionMode.PLAN else "ask"
        if tool in PURE_TOOLS:
            base = "allow"
        rule = evaluate(tool, "*", ruleset, default=base)
        dec = Decision(action=rule.action, patterns=["*"], rule=rule)
        if mode == PermissionMode.PLAN and tool not in PURE_TOOLS:
            dec.action, dec.message = "deny", f"Plan mode is read-only; {tool} is unavailable: {PLAN_HINT}"

    return _finish(dec, mode, headless)


#: What a call that only needs a yes gets in a run nobody can ask (`-p`); `k3code -p` counts these for its closing note.
HEADLESS_ASK_DENIED = "Permission denied: approval required but unavailable (headless mode)."


def _finish(dec: Decision, mode: PermissionMode, headless: bool) -> Decision:
    if dec.hardline is None and dec.action != "deny" and not dec.always_ask:
        if mode == PermissionMode.YOLO:
            dec.action = "allow"
        elif mode == PermissionMode.AUTO and dec.action == "ask" and not dec.needs_human:
            dec.action, dec.auto_allowed = "allow", True
    if dec.action == "ask" and headless:
        dec.action = "deny"
        # a specific reason (a path outside the project, a credential file) stays first; the model also hears that
        # nobody can answer, and `k3code -p` counts the refusal by that sentence
        stop = "" if (dec.message or ".").endswith(".") else "."
        dec.message = f"{dec.message}{stop} {HEADLESS_ASK_DENIED}" if dec.message else HEADLESS_ASK_DENIED
    if dec.action == "deny" and not dec.message:
        dec.message = f"Denied by rule {dec.rule.tool}:{dec.rule.pattern}" if dec.rule else "Denied."
    return dec


#: The redirection patterns are defined next to the parser (hardline.py): ``>``, ``>>``, ``>|``, ``<>``, ``&>``, ``<``.
_HARMLESS_REDIRECT = hardline.HARMLESS_REDIRECT
_REDIRECT = hardline.REDIRECT
_WRITE_REDIRECT = hardline.WRITE_REDIRECT
_SUBSTITUTION = re.compile(r"\$\(|`")
#: Files that run as code or hold credentials, wherever they sit (a nested repo's hooks too): shell startup files, ssh /
#: gpg / aws keys, git hooks / config / info (``core.fsmonitor`` and ``core.hooksPath`` run programs), husky hooks,
#: direnv's ``.envrc``, dotenv files.
_CODE_NAMES = (
    r"\.ssh|\.gnupg|\.git/(?:[^/]+/)*(?:hooks|info|config(?:\.worktree)?)|\.husky|\.envrc|\.aws|\.bash_?(?:rc|_profile)|"
    r"\.zsh(?:rc|env)|\.profile|\.zprofile|authorized_keys|\.netrc|\.npmrc|\.env"
)
#: Redirect targets that are code or credentials even inside the project (``.k3code`` too: its config holds the
#: permission rules and hooks).
_SENSITIVE_TARGET = re.compile(rf"(?:^|/)(?:{_CODE_NAMES}|\.k3code)(?:/|$)", re.IGNORECASE)
#: The same for the edit tools, which run in the daemon outside the sandbox (see protected_path): ``.k3code`` is only
#: its config (agents, skills and plans in there are ordinary files an agent writes).
_CODE_LOCATION = re.compile(rf"(?:^|/)(?:{_CODE_NAMES}|\.k3code/config[^/]*)(?:/|$)", re.IGNORECASE)


def _writes_outside_roots(sub: str, roots: list[str], cwd: str) -> bool:
    """A redirect in ``sub`` writes a file outside the project roots. Targets that are variables or globs are left to
    the sandbox; the ``/dev`` sinks are not files."""
    plain = _HARMLESS_REDIRECT.sub("", sub)
    for target in _WRITE_REDIRECT.findall(plain):
        target = target.strip("'\"")
        if not target or target.startswith("/dev/") or any(ch in target for ch in "$`*?[{"):
            continue
        if not _inside(_abs(target, cwd), roots):
            return True
    return False


def _redirects_ok(plain: str, roots: list[str], cwd: str) -> bool:
    """Every redirect target is a plain path inside the project roots and not a credential/startup file."""
    for target in _REDIRECT.findall(plain):
        target = target.strip("'\"")
        if not target or any(ch in target for ch in "$*?[{~") and not target.startswith("~/"):
            return False  # a variable or glob: unknown target
        path = _abs(target, cwd)
        if not _inside(path, roots) or any(_SENSITIVE_TARGET.search(p) for p in (path, os.path.realpath(path))):
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
        return has_redirect or has_subst or any(_arg_outside(a, roots, cwd) for a in _path_args(sub))
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


def _argv(sub: str, reads: list[str] | None = None, legacy: bool = False) -> list[str]:
    """The normalised argv of ``sub`` (wrappers, assignments and redirections dropped; see hardline.normalize_argv).
    ``reads`` collects the files a dropped wrapper option reads (``xargs -a FILE``). ``legacy``: the reading from
    before the reserved words and the newer wrappers were known."""
    words = hardline.strip_redirects(sub, heredocs=False, legacy=legacy)  # redirect targets are checked separately
    return hardline.normalize_argv(hardline.tokens(words), reads, legacy=legacy)


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


# ── credential files named on a command line ──

#: ``<file`` and ``<>file`` (not ``<<``, ``<<<``, ``<&``, ``<(``): a file the command reads.
_INPUT_REDIRECT = re.compile(r"(?:^|[^<>&\d])\d*<>?(?![<&(])\s*([^\s;&|<>()]+)")
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
#: container one. Both are an ask that a person answers.
_SEVERITY = {"ask": 1, "human": 2, "confirm": 2, "deny": 3}


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
    base = parts[-1].lower()
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


def _glob_reads_secret(pattern: str, home: str) -> bool:
    """An absolute glob that matches a credential file: on disk, by its literal leading directory, or by its shape
    against the canonical secret names."""
    for k, hit in enumerate(glob.iglob(pattern)):
        if sensitive_path(hit):
            return True
        if k >= 1000:
            break
    literal: list[str] = []
    for seg in pattern.split("/"):
        if _GLOB_CHARS & set(seg):
            break
        literal.append(seg)
    base = "/".join(literal) or "/"
    if base != "/" and sensitive_path(base):
        return True
    return any(_glob_match(pattern, c) for c in _canonical_secrets(pattern, home))


def _classify_arg(arg: str, cwd: str, home: str) -> str | None:
    """``deny`` when ``arg`` names (or globs onto) a credential file, ``human`` when an unknown variable stands in
    front of a credential name (``$DIR/.env``), else None. ``{a,b}`` alternatives are classified one by one."""
    worst: str | None = None
    for word in hardline.brace_expand(arg):
        worst = _worse(worst, _classify_word(word, cwd, home))
    return worst


def _classify_word(arg: str, cwd: str, home: str) -> str | None:
    arg = arg.strip("'\"")
    if not arg or "://" in arg:
        return None
    word = hardline.expand_vars(arg, cwd, home)
    if "$" in word or "`" in word:
        return "human" if _sensitive_name(_ANY_VAR.sub("", word)) else None
    path = _abs(word, cwd)
    if _GLOB_CHARS & set(word):
        return "deny" if _glob_reads_secret(path, home) else None
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


#: Commands that open the files named in their arguments. One of those arguments being a shell expansion whose value
#: is not known (``cat $(echo .e)nv``, ``head $f``) may name a credential file, so a person decides (see _reader_files).
_FILE_READERS = frozenset(
    {
        *("cat", "tac", "head", "tail", "less", "more", "nl", "od", "xxd", "hexdump", "strings", "base64", "base32"),
        *("sort", "uniq", "cut", "paste", "wc", "diff", "cmp", "comm", "jq", "yq", "bat", "view", "source", "."),
        *("tar", "zip", "unzip", "gzip", "gunzip", "zcat", "bzip2", "xz", "7z", "rsync", "scp"),
        *("grep", "egrep", "fgrep", "rg", "sed", "awk", "gawk", "mawk"),
        *_COPIERS,
    }
)
_GREP_LONG = frozenset(
    {"--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context", "--directories"}
    | {"--devices", "--include", "--exclude", "--exclude-from", "--exclude-dir", "--label", "--binary-files"}
)
_RG_LONG = frozenset(
    {"--regexp", "--file", "--glob", "--iglob", "--type", "--type-not", "--max-count", "--max-depth", "--context"}
    | {"--after-context", "--before-context", "--replace", "--threads", "--max-columns", "--encoding", "--pre"}
    | {"--pre-glob", "--sort", "--sortr", "--ignore-file", "--type-add", "--colors"}
)
#: Commands whose first operand is the pattern or program, not a file (``grep PATTERN FILE``, ``sed SCRIPT FILE``):
#: (short options with a value, long options with a value, options that give the pattern or program, options whose
#: value is a file to read).
_PATTERN_FIRST: dict[str, tuple[str, frozenset[str], frozenset[str], frozenset[str]]] = {
    **dict.fromkeys(
        ("grep", "egrep", "fgrep"),
        ("efmABCdD", _GREP_LONG, frozenset({"-e", "-f", "--regexp", "--file"}), frozenset({"-f", "--file"})),
    ),
    "rg": ("ABCEMTdefgjmrt", _RG_LONG, frozenset({"-e", "-f", "--regexp", "--file"}), frozenset({"-f", "--file"})),
    "sed": (
        "efl",
        frozenset({"--expression", "--file", "--line-length"}),
        frozenset({"-e", "-f", "--expression", "--file"}),
        frozenset({"-f", "--file"}),
    ),
    **dict.fromkeys(
        ("awk", "gawk", "mawk"),
        (
            "fFvWe",
            frozenset({"--file", "--field-separator", "--assign", "--source"}),
            frozenset({"-f", "-e", "--file", "--source"}),
            frozenset({"-f", "--file"}),
        ),
    ),
}


def _reader_files(name: str, args: list[str]) -> list[str]:
    """The operands of ``name args`` that are files it opens (see _FILE_READERS)."""
    if name not in _FILE_READERS:
        return []
    if name in _COPIERS:
        return _copier_roles(args)[0]
    spec = _PATTERN_FIRST.get(name)
    if spec is None:
        return _words(args)
    short_val, long_val, script_opts, file_opts = spec
    opts, positional = _options(args, short_val, long_val)
    files = [v for o, v in opts if o in file_opts and v]
    return files + (positional if any(o in script_opts for o, _ in opts) else positional[1:])


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
    return _words(args), [], []


def _sub_secret_access(
    sub: str, piped: bool, cwd: str, home: str, env: dict[str, list[str]] | None = None
) -> str | None:
    """``sub`` as written and, when it uses a variable the command line assigns, with the values put in
    (``f=.env; cat $f`` reads ``.env``); each also with its ``$'...'`` words decoded, as bash runs them
    (``cat $'\\x2eenv'`` reads ``.env``)."""
    worst: str | None = None
    for text in hardline.expand_assigned(sub, env or {}):
        for spelling in dict.fromkeys((text, hardline.decode_ansi_c(text)) if "$'" in text else (text,)):
            worst = _worse(worst, _text_secret_access(spelling, piped, cwd, home))
    return worst


def _text_secret_access(sub: str, piped: bool, cwd: str, home: str) -> str | None:
    plain = _HARMLESS_REDIRECT.sub("", sub)
    worst: str | None = None
    for source in _INPUT_REDIRECT.findall(plain):
        worst = _worse(worst, _classify_arg(source, cwd, home))
    seen: list[list[str]] = []
    for legacy in (False, True):  # the command as the normaliser reads it now, and as it did before it knew more
        wrapper_reads: list[str] = []  # ``xargs -a .env echo`` prints the file whatever it wraps
        argv = _argv(sub, wrapper_reads, legacy)
        if argv in seen:
            continue
        seen.append(argv)
        for arg in wrapper_reads:
            worst = _worse(worst, _classify_arg(arg, cwd, home))
        if argv:
            worst = _worse(worst, _argv_secret_access(argv, piped, cwd, home))
    return worst


def _argv_secret_access(argv: list[str], piped: bool, cwd: str, home: str) -> str | None:
    worst: str | None = None
    for command in hardline.ssh_option_commands(argv[0], argv[1:]):
        # ``ssh -oProxyCommand='head .env' x``: ssh runs it, and its output goes on to ssh
        worst = _worse(worst, _secret_access(hardline.parse(command), cwd, home, consumed=True))
    reads, writes, confirm = _credential_roles(argv[0], argv[1:], piped)
    for arg in reads:
        worst = _worse(worst, _classify_arg(arg, cwd, home))
    for arg in _reader_files(argv[0], argv[1:]):
        if hardline.UNRESOLVED.search(hardline.expand_vars(arg.strip("'\""), cwd, home)):
            worst = _worse(worst, "human")  # an expansion whose value is unknown may name a credential file
    if any(_classify_arg(arg, cwd, home) for arg in writes):
        worst = _worse(worst, "ask")
    if any(_classify_arg(arg, cwd, home) for arg in confirm):
        worst = _worse(worst, "confirm")
    return worst


def _secret_access(
    parsed: hardline.Parsed, cwd: str, home: str, consumed: bool = False, env: dict[str, list[str]] | None = None
) -> str | None:
    """How a command line touches credential files: ``deny`` (reads, copies or sends one), ``human`` (a variable may
    point at one), ``confirm`` (deletes, sources, stages one or hands it to a container), ``ask`` (overwrites one),
    None. Every argument of every simple command and of everything nested in it counts unless the command only prints,
    tests or authenticates with it (see _credential_roles), and so do ``<`` sources; ``$HOME``, ``$PWD`` and ``~`` are
    expanded, globs are matched."""
    worst: str | None = None
    for sub in parsed.subs:
        piped = consumed or any(len(p) > 1 and sub in p for p in parsed.pipelines)
        worst = _worse(worst, _sub_secret_access(sub, piped, cwd, home, env))
    # the first children are ``$(...)``/backtick/``<(...)`` payloads: their output becomes the outer arguments
    n_subst = sum(len(ps) for ps in parsed.sub_payloads)
    for k, child in enumerate(parsed.children):
        worst = _worse(worst, _secret_access(child, cwd, home, consumed or k < n_subst, env))
    return worst


def _rule_forms(parsed: hardline.Parsed) -> list[str]:
    """What else the subs of ``parsed`` run as, for deny/ask rules: their normalised argv (``/bin/rm x`` is ``rm x``,
    ``git -C . push`` is ``git push``) and every nested command (``bash -c '...'``, ``$(...)``) in both spellings."""
    forms: list[str] = []
    for sub in parsed.subs:
        for legacy in (False, True):  # also as the normaliser read it before it knew reserved words and more wrappers
            norm = " ".join(_argv(sub, legacy=legacy))
            if norm and norm != sub:
                forms.append(norm)
    for nested in parsed.nested:
        forms.append(nested)
        for legacy in (False, True):
            norm = " ".join(_argv(nested, legacy=legacy))
            if norm and norm != nested:
                forms.append(norm)
    return list(dict.fromkeys(forms))


def _force_push_ask(why: str, prefixes: list[str], command: str) -> Decision:
    return Decision(
        action="ask",
        patterns=prefixes,
        needs_human=True,
        always_ask=True,  # yolo asks too: this is the one command it would rewrite shared history with
        message=f"{why}; confirm it yourself: {command[:120]}",
    )


#: What a hardline denial adds for the rules that have a way to do the same thing legitimately.
_HARDLINE_HINTS = {
    "env-dump": " (it prints secrets: set a variable with `VAR=x cmd`, read a plain one with `printenv NAME`)",
}


def _decide_bash(
    mode: PermissionMode, command: str, ruleset: list[Rule], extra: list[str] | None, roots: list[str], cwd: str
) -> Decision:
    home = os.path.expanduser("~")
    hit = hardline.check(command, extra, cwd=cwd, home=home)
    if hit:
        message = f"Hardline deny ({hit}): {command[:120]}{_HARDLINE_HINTS.get(hit, '')}"
        return Decision(action="deny", message=message, hardline=hit)
    parsed = hardline.parse(command)
    if parsed.too_deep and mode == PermissionMode.YOLO:
        # nested past the parse limit: the hardline could not look inside, and yolo has nobody to ask
        hit = "nesting-too-deep"
        return Decision(action="deny", message=f"Hardline deny ({hit}): {command[:120]}", hardline=hit)
    if mode == PermissionMode.YOLO and hardline.capped(parsed, cwd, home):
        # past an expansion cap the hardline judged only some spellings, and yolo has nobody to ask
        hit = "expansion-capped"
        return Decision(action="deny", message=f"Hardline deny ({hit}): {command[:120]}", hardline=hit)
    subs = parsed.subs
    # an open quote or a quote inside a comment: the shell may not run what was parsed, so no rule vouches for it.
    # Same for what the line leaves open: a shell reading a script that is not on it, a command or rm argument that is
    # an expansion whose value is unknown (`$cmd -rf /`, `rm -rf $(echo /)`), nesting past the parse limit.
    unparsed = hardline.needs_human(parsed, cwd, home)
    unsafe = parsed.unterminated or parsed.comment_quote or bool(unparsed)
    secret = _secret_access(parsed, cwd, home, env=hardline.assignments(parsed))
    if secret == "deny":
        hit = "sensitive-path"
        return Decision(action="deny", message=f"Hardline deny ({hit}): {command[:120]}", hardline=hit)
    prefixes = [_rule_pattern_base(s) for s in subs]
    if mode == PermissionMode.PLAN:
        return Decision(action="deny", patterns=prefixes, message=PLAN_MSG)
    force = hardline.risky(parsed)
    if mode == PermissionMode.YOLO:
        return _force_push_ask(force, prefixes, command) if force else Decision(action="allow", patterns=prefixes)
    if mode == PermissionMode.AUTO and any(_writes_outside_roots(sub, roots, cwd) for sub in subs):
        # auto mode writes only inside the project roots; no allow rule overrides this
        return Decision(
            action="deny", patterns=prefixes, message=f"Auto mode writes only inside the project roots: {command[:120]}"
        )
    worst: Rule | None = None
    for k, sub in enumerate([*subs, *parsed.legacy_subs]):  # the legacy readings have no heredoc bookkeeping
        rule = evaluate("bash", sub, ruleset, default="ask")
        # no rule vouches for a program fed to an interpreter through a heredoc (`bash <<EOF`, `cat <<EOF | sh`): the
        # command line does not say what runs
        own = k < len(subs)
        if rule.action == "allow" and (
            (own and parsed.code_heredoc[k])
            or _voids_allow(sub, rule, ruleset, roots, cwd, parsed.heredocs[k] if own else None)
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
        detail = f" ({unparsed})" if unparsed else ""
        return Decision(
            action="ask",
            patterns=prefixes,
            needs_human=True,
            message=f"Command could not be parsed safely{detail}; confirm it yourself: {command[:120]}",
        )
    if force and worst.action != "deny":
        return _force_push_ask(force, prefixes, command)
    if secret in ("human", "confirm") and worst.action != "deny":
        why = (
            "A variable may point at a credential file"
            if secret == "human"
            else "Deletes, sources, stages or hands a container a credential file"
        )
        return Decision(
            action="ask", patterns=prefixes, needs_human=True, message=f"{why}; confirm it yourself: {command[:120]}"
        )
    if secret == "ask" and worst.action == "allow":
        return Decision(action="ask", patterns=prefixes, message=f"Overwrites a credential file: {command[:120]}")
    if worst.action == "allow" and not _inside(cwd, roots):
        return Decision(action="ask", patterns=prefixes, message=f"Working directory outside project roots: {cwd}")
    return Decision(action=worst.action, patterns=prefixes, rule=worst)


def _decide_path(mode: PermissionMode, tool: str, path: str, roots: list[str], ruleset: list[Rule]) -> Decision:
    is_edit = tool in EDIT_TOOLS
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
        if rule.layer == 0:
            dec.message = f"Outside project roots: {path}"
        if is_edit and mode == PermissionMode.AUTO and rule.action == "ask":
            # auto mode writes only inside the project roots; an explicit user allow rule still applies
            dec.action, dec.message = "deny", f"Auto mode writes only inside the project roots: {path}"
        elif not is_edit and rule.action == "ask":
            # an in-process read outside the roots is never auto-allowed: a person approves it (or adds a read rule)
            dec.needs_human = True
    return dec


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


def sensitive_path(path: str) -> bool:
    """``path`` (absolute) is a key/credential location: ``~/.ssh``, ``~/.gnupg``, ``~/.aws``, the k3code secrets dir
    (``~/.config/k3code``, ``$XDG_CONFIG_HOME/k3code``: the daemon's ``env`` file), a process environment
    (``/proc/*/environ``) or a dotenv file. Checked on the path as written and on its realpath (symlinks), and
    case-insensitively: on drvfs (``/mnt/c``) and macOS ``.ENV`` and ``~/.SSH/id_rsa`` open the same files."""
    dirs = [os.path.normpath(d) for d in _secret_dirs()]
    dirs = [d.lower() for d in dict.fromkeys(dirs + [os.path.realpath(d) for d in dirs])]
    for p in dict.fromkeys((os.path.normpath(path), os.path.realpath(path))):
        folded = p.lower()
        if _PROC_ENVIRON.match(p) or any(folded == d or folded.startswith(d.rstrip("/") + "/") for d in dirs):
            return True
        if _dotenv_name(os.path.basename(p)):
            return True
    return False


def grep_excludes(base: str) -> list[str]:
    """rg globs (``--iglob``, to come after every ``include`` glob: the last one wins) that keep credential files out of
    a search. An include glob switches rg's hidden-file filter off, so ``include: "*"`` would search ``.env``; these
    are the files and directories ``sensitive_path`` calls sensitive: dotenv files at any depth, and the secret
    directories under ``base`` (the search root), anchored there."""
    globs = ["!.env", "!.env.*"]
    secrets = [*map(os.path.normpath, _secret_dirs()), *map(os.path.realpath, _secret_dirs())]
    for root in dict.fromkeys((os.path.normpath(base), os.path.realpath(base))):
        prefix = root.rstrip("/") + "/"
        globs += ["!/" + d[len(prefix) :] for d in dict.fromkeys(secrets) if d.startswith(prefix)]
    return list(dict.fromkeys(globs))


def _glob_base(path: str, pattern: str) -> str:
    """The directory a glob of ``pattern`` under ``path`` starts from: its literal leading segments count, so a
    pattern such as ``../../.ssh/*`` is decided as ``~/.ssh``."""
    if pattern.startswith("~"):  # tool_glob expands it: ``~/.ssh/*`` is the home directory's, not a folder named ``~``
        pattern = os.path.expanduser(pattern)
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
    arguments name what runs (hardline.WILDCARD_UNSAFE: ``uv run pytest``, ``docker run --rm alpine``)."""
    sub = hardline.strip_leading_reserved(sub)  # `then git push`: the rule is for `git push`, never for `then *`
    if hardline.wildcard_unsafe(sub):
        return sub
    return hardline.command_prefix(sub) or sub


def _carries_secret(pattern: str) -> bool:
    """``pattern`` has a credential in it (a token flag, ``NAME_SECRET=value``, an auth header ...): see redact.py."""
    from k3code.redact import scrub_text

    return scrub_text(pattern) != pattern


def exact_rule_pattern(command: str) -> str | None:
    """``command`` as an exact rule pattern (leading assignments dropped), or None when it cannot be one: it has a
    wildcard, variable, substitution or redirection in it, or carries a secret (an exact rule is written to
    config.yaml verbatim, and ``npx tool --token sk-...`` would put the token there)."""
    command = _drop_assignments(command.strip())
    return command if command and not _INEXACT.search(command) and not _carries_secret(command) else None


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
            if not p.strip() or hardline.is_launcher(p) or p.split()[0] in hardline.RESERVED_WORDS:
                continue
            pattern = exact_rule_pattern(p) if hardline.wildcard_unsafe(p) else f"{p} *"
            if pattern and not _carries_secret(pattern):
                out.append(Rule(tool="bash", pattern=pattern, action="allow"))
        return out
    name = "edit" if tool in EDIT_TOOLS else "read" if tool in READ_TOOLS else tool
    return [Rule(tool=name, pattern=p, action="allow") for p in dec.patterns]
