"""The permission engine in a real coding session: no prompt for non-invasive work (in a project and at ``$HOME``),
and the holes the security review of the allowlist found stay closed."""

from __future__ import annotations

import pytest

from k3code.permissions import PermissionMode, Rule, decide
from k3code.permissions.engine import home_read_only

# ── security review of the read-only / sandboxed allowlist ──

HANGS_OR_RUNS = [
    # yq v4 `eval` builds `load`/`strenv` at runtime: the name never appears in the program
    'yq -n \'eval("lo"+"ad_str(\\"/home/x/.ssh/id_ed25519\\")")\'',
    'yq -n \'eval("str"+"en"+"v(HOME)")\'',
    "yq eval .a config.yaml",
    # follows forever: the tool call never returns
    "tail -f log.txt",
    "tail -F log.txt",
    "tail --follow=name log.txt",
    "tail -n5 -f log.txt",
    "tail --fol log.txt",
]


@pytest.mark.parametrize("command", HANGS_OR_RUNS)
@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS])
def test_yq_eval_and_tail_follow_ask(command: str, mode: PermissionMode, tmp_path) -> None:
    dec = decide(mode=mode, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "ask", (command, dec)


@pytest.mark.parametrize(
    "command",
    ["make test install", "make test deploy", "make install test", "make test -- install", "make check lint publish"],
)
def test_make_runs_only_the_listed_targets(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.ACCEPT_EDITS, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "ask" and not dec.sandbox_only, command


@pytest.mark.parametrize("command", ["make test", "make check lint", "make test V=1", "make test -j4"])
def test_make_with_the_listed_targets_stays_a_sandboxed_allow(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.ACCEPT_EDITS, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "allow" and dec.sandbox_only, (command, dec)


@pytest.mark.parametrize(
    "command",
    [
        "git show HEAD:.env.production",
        "git show :.env",
        "git cat-file -p HEAD:.env",
        "git show main:config/.env.local",
        "git show HEAD:./.env",
        "git diff HEAD:.env HEAD:.env.example",
    ],
)
def test_git_rev_path_to_a_credential_file_is_denied(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.DEFAULT, tool="bash", args={"command": command}, cwd=tmp_path)
    # `git cat-file` trips the older `dotenv-cat` regex (`\bcat\b`) first: any hardline name is a deny
    assert dec.action == "deny" and dec.hardline is not None, (command, dec)


@pytest.mark.parametrize(
    "command",
    [
        "git show HEAD:README.md",
        "git show HEAD:",
        "git show HEAD:.env.example",
        "git commit -m 'fix: .env handling'",
        "git remote add origin git@github.com:user/repo.git",
    ],
)
def test_git_rev_path_to_other_files_is_no_credential(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.DEFAULT, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.hardline is None, (command, dec)


def test_protected_project_entries_are_bound_read_only_in_the_sandbox(tmp_path) -> None:
    from k3code.reliability import sandbox

    home = tmp_path / "home"
    proj = home / "proj"
    for d in (proj / ".git", proj / ".husky", proj / ".k3code"):
        d.mkdir(parents=True)
    for f in (".envrc", ".gitattributes", ".gitmodules"):
        (proj / f).write_text("x\n")
    (proj / ".k3code" / "config.yaml").write_text("permissions: {}\n")
    (proj / ".k3code" / "config.local.yaml").write_text("")
    (proj / ".k3code" / "sessions.json").write_text("{}")
    joined = " ".join(sandbox.build_argv(proj, home=home, bwrap="/usr/bin/bwrap"))
    bound = joined.index(f"--bind {proj} {proj}")
    for rel in (
        ".husky",
        ".envrc",
        ".gitattributes",
        ".gitmodules",
        ".k3code/config.yaml",
        ".k3code/config.local.yaml",
    ):
        p = proj / rel
        assert f"--ro-bind {p} {p}" in joined, rel
        assert joined.index(f"--ro-bind {p} {p}") > bound, rel  # after the project bind, so it wins
    assert f"--ro-bind {proj / '.k3code' / 'sessions.json'}" not in joined  # only the config is protected


def test_missing_protected_entries_are_skipped(tmp_path) -> None:
    from k3code.reliability import sandbox

    (tmp_path / "proj").mkdir()
    assert sandbox.protected_entries(tmp_path / "proj") == []


# ── no prompt for non-invasive work ──


@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS])
def test_toolchain_reads_never_prompt_in_any_mode(mode: PermissionMode, tmp_path) -> None:
    site = "/srv/venv/lib/site-packages/requests/api.py"
    assert decide(mode=mode, tool="read", args={"path": site}, cwd=tmp_path).action == "allow"
    usr = decide(mode=mode, tool="grep", args={"path": "/usr/lib/python3", "pattern": "x"}, cwd=tmp_path)
    assert usr.action == "allow"
    # anything else outside the roots still asks a person (accept-edits reads it: a non-secret path); a user rule
    # still wins
    d = decide(mode=mode, tool="read", args={"path": "/tmp/elsewhere.py"}, cwd=tmp_path)
    if mode == PermissionMode.ACCEPT_EDITS:
        assert d.action == "allow" and not d.needs_human
    else:
        assert d.action == "ask" and d.needs_human
    user = [Rule(tool="read", pattern="/srv/venv/*", action="ask")]
    assert decide(mode=mode, tool="read", args={"path": site}, cwd=tmp_path, user_rules=user).action == "ask"
    dotenv = decide(mode=mode, tool="read", args={"path": "/srv/venv/lib/site-packages/.env"}, cwd=tmp_path)
    assert dotenv.action == "deny"


@pytest.mark.parametrize(
    ("command", "ok"),
    [
        ("head -n 20 src/a.py", True),
        ("wc -l src/a.py", True),
        ("git status", True),
        ("git log --oneline -10", True),
        ("git show HEAD", False),  # prints file contents: a $HOME repo may track dotenv/.bashrc
        ("git diff", False),
        ("git blame .bashrc", False),
        ("git log -p", False),
        ("git log --patch", False),
        ("git log --pat", False),
        ("git branch -a", True),
        ("git branch -r", True),
        ("pwd", True),
        ("which python3", True),
        ("cut -d: -f1 names.txt", True),
        ("sort -r names.txt", True),
        ("diff a.txt b.txt", True),
        ("git log --oneline | head -5", True),
        ("python3 --version", False),  # a launcher: `ls; python3 x` must never ride on the `ls` allow
        ("diff -r a b", False),
        ("diff --recursive a b", False),
        ("git diff --no-index a b", False),
        ("rg KEY", False),
        ("find . -name x", True),  # find lists names; the sandbox masks the credential dirs
        ("find . -name '*.py' -type f", True),
        ("find . -delete", False),  # a write/exec primary still refuses it
        ("find . -exec cat {} ;", False),
        ("tree -L 2", False),
        ("du -sh .", False),
        ("jq .a x.json", False),
        ("grep -rn KEY .", False),
        ("ls -R", False),
    ],
)
def test_home_read_only_covers_the_plain_readers_but_no_tree_walker(command: str, ok: bool) -> None:
    assert home_read_only(command) is ok, command


class _Router:
    chain: list = []


class _Answer:
    def __init__(self, allowed: bool) -> None:
        self.allowed, self.reason = allowed, ""


def _home_loop(home, monkeypatch, mode: str):
    from k3code.agent.loop import AgentLoop
    from k3code.reliability import sandbox

    monkeypatch.setenv("HOME", str(home))
    built: list[str] = []
    asked: list[str] = []
    monkeypatch.setattr(sandbox, "usable", lambda: True)
    monkeypatch.setattr(
        sandbox,
        "build_argv",
        lambda cwd, *a, readonly_home=False, **k: (
            built.append("ro-home" if readonly_home else "bwrap") or ["/usr/bin/env"]
        ),
    )

    async def approve(name, args, decision):
        asked.append(decision.message or "")
        return _Answer(False)

    loop = AgentLoop(_Router(), system_prompt="x", cwd=home, permission_mode=mode)  # type: ignore[arg-type]
    loop.approval_callback = approve
    loop.headless = False

    async def ran(args, **kw):
        return {"ok": True}

    spec, _ = loop.tools.get("bash")
    loop.tools._tools["bash"] = (spec, ran)
    return loop, built, asked


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "head -n 20 src/a.py",
        "git log --oneline | head -5",
        "pwd",
        "grep -n 'foo bar' notes",  # a quoted literal with a space: no prompt now
        'grep -n "foo bar" notes',
        "find . -name '*.py'",  # find listing names, with a quoted glob
        "find . -type f",
    ],
)
async def test_auto_mode_at_home_runs_plain_readers_in_the_read_only_home_sandbox(
    command: str, tmp_path, monkeypatch
) -> None:
    from k3code.providers.types import ToolCall

    loop, built, asked = _home_loop(tmp_path, monkeypatch, "auto")
    out = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": command}))
    assert asked == [] and built == ["ro-home"], (command, out)


@pytest.mark.parametrize("command", ["cat *.py", 'cat "$SECRET"', "find . -delete", "grep -rn KEY ."])
async def test_auto_mode_at_home_still_asks_for_globs_variables_and_risky_find(
    command: str, tmp_path, monkeypatch
) -> None:
    # an unquoted glob/variable, a tree walker or a find that writes/execs is not run in the read-only home sandbox
    from k3code.providers.types import ToolCall

    loop, built, asked = _home_loop(tmp_path, monkeypatch, "auto")
    await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": command}))
    assert built != ["ro-home"], (command, built)


async def test_auto_mode_at_home_still_asks_for_a_tree_walker(tmp_path, monkeypatch) -> None:
    from k3code.providers.types import ToolCall

    loop, built, asked = _home_loop(tmp_path, monkeypatch, "auto")
    await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": "rg KEY"}))
    assert len(asked) == 1 and "home directory" in asked[0] and built == []


def _record_bash(loop) -> list:
    """Swap the home loop's bash handler for one that records the sandbox argv it is handed (None = unsandboxed)."""
    spec, _ = loop.tools.get("bash")
    seen: list = []

    async def ran(args, **kw):
        seen.append(kw.get("sandbox", "missing"))
        return {"ok": True}

    loop.tools._tools["bash"] = (spec, ran)
    return seen


@pytest.mark.parametrize("command", ["touch x", "echo hi > note.txt", "rg KEY", "git diff"])
async def test_yolo_at_home_runs_every_command_unsandboxed_without_asking(command: str, tmp_path, monkeypatch) -> None:
    # yolo is the no-prompt mode (README): no sandbox can contain $HOME, so it runs on the host like it does when
    # bwrap is unusable, instead of asking per command as auto does
    from k3code.providers.types import ToolCall

    loop, built, asked = _home_loop(tmp_path, monkeypatch, "yolo")
    seen = _record_bash(loop)
    out = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": command}))
    assert out.get("ok") is True and "error" not in out, (command, out)
    assert asked == [] and built == [] and seen == [None], (command, asked, built, seen)


async def test_auto_at_home_still_asks_where_yolo_runs(tmp_path, monkeypatch) -> None:
    from k3code.providers.types import ToolCall

    loop, built, asked = _home_loop(tmp_path, monkeypatch, "auto")
    seen = _record_bash(loop)
    out = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": "touch x"}))
    assert len(asked) == 1 and "home directory" in asked[0], asked
    assert "User denied" in out.get("error", "") and seen == [], (out, seen)


async def test_yolo_at_home_still_refuses_the_hardline_list(tmp_path, monkeypatch) -> None:
    from k3code.providers.types import ToolCall

    loop, built, asked = _home_loop(tmp_path, monkeypatch, "yolo")
    seen = _record_bash(loop)
    out = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": "rm -rf ~"}))
    assert "error" in out and asked == [] and seen == [], (out, asked, seen)


async def test_unattended_yolo_at_home_is_still_refused(tmp_path, monkeypatch) -> None:
    # nobody is there to see an unsandboxed run: bash at $HOME stays refused for background/sub-agent work
    from k3code.providers.types import ToolCall

    loop, built, asked = _home_loop(tmp_path, monkeypatch, "yolo")
    loop.unattended = True
    seen = _record_bash(loop)
    out = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": "touch x"}))
    assert "bash refused" in out.get("error", "") and asked == [] and seen == [], (out, asked, seen)


@pytest.mark.parametrize("command", ["git show HEAD", "git diff", "git log -p", "git log --patch"])
async def test_auto_mode_at_home_asks_for_git_commands_that_print_file_contents(
    command: str, tmp_path, monkeypatch
) -> None:
    from k3code.providers.types import ToolCall

    loop, built, asked = _home_loop(tmp_path, monkeypatch, "auto")
    await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": command}))
    assert len(asked) == 1 and built == [], command
