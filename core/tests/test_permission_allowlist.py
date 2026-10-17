"""The builtin read-only bash allowlist and the sandboxed test/build/lint allowlist of accept-edits and auto mode."""

from __future__ import annotations

import pytest

from k3code.permissions import PermissionMode, Rule, decide

READ_ONLY = [
    "pwd",
    "head -n 5 README.md",
    "tail -n 20 log.txt",
    "wc -l src/a.py",
    "file README.md",
    "stat README.md",
    "du -sh .",
    "df -h",
    "which python3",
    "type ls",
    "command -v git",
    "whoami",
    "id -u",
    "uname -a",
    "date +%s",
    "date -Iseconds",
    "tree -L 2",
    "find . -name '*.py' -type f",
    "sort -u names.txt",
    "uniq -c names.txt",
    "cut -d: -f1 names.txt",
    "tr a-z A-Z",
    "jq .name package.json",
    "yq .version config.yaml",
    "diff a.txt b.txt",
    "cmp a.txt b.txt",
    "sha256sum README.md",
    "basename src/a.py",
    "dirname src/a.py",
    "realpath src",
    "readlink -f src",
    "git status --short",
    "git log --oneline -5",
    "git diff --stat",
    "git show HEAD",
    "git branch",
    "git branch -a",
    "git branch -vv",
    "git branch --show-current",
    "git branch --list 'feat/*'",
    "git branch --contains HEAD",
    "git tag",
    "git tag -l 'v*'",
    "git tag -n5 --list",
    "git remote -v",
    "git remote get-url origin",
    "git rev-parse --show-toplevel",
    "git blame README.md",
    "git ls-files",
    "git describe --tags",
    "python --version",
    "node --version",
    "go version",
    "cargo --version",
    "git log --oneline | head -3",
]

#: Variants that write, run a program, change refs or read the environment: they ask.
DANGEROUS = [
    "find . -exec rm {} ;",
    "find . -name x -execdir sh -c id ;",
    'find . "-exec" rm {} ;',
    "find . -ok rm {} ;",
    "find . -delete",
    "find . -fprint out.txt",
    "sort -o out.txt names.txt",
    "sort -uo out.txt names.txt",
    "sort --out=out.txt names.txt",
    "sort --compress-program=sh names.txt",
    "tree -o out.txt",
    "tree -ao out.txt",
    "file -C",
    "file --compile",
    "date -s 10:00",
    "date --set=10:00",
    "uniq names.txt out.txt",
    "diff --output=x a b",
    "jq -n env",
    "jq -n '$ENV.HOME'",
    "jq -f prog.jq data.json",
    "jq -L lib 'import \"x\" as x; .'",
    "yq -i .a=1 config.yaml",
    "yq -s .a config.yaml",
    "yq 'strenv(HOME)'",
    "yq 'load(\"x.yaml\")'",
    "git show --output=x HEAD",
    "git show --ext-diff HEAD",
    "git diff --out=x",
    "git blame --textconv README.md",
    "git branch newbranch",
    "git branch -d old",
    "git branch -D old",
    "git branch -m a b",
    "git branch -f main HEAD~1",
    "git branch --set-upstream-to=origin/x",
    "git branch -a newbranch",
    "git tag v1.0",
    "git tag -d v1.0",
    "git tag -a v1 -m x",
    "git tag -f v1",
    "git remote add x y",
    "git remote -v add x y",
    "git remote set-url origin x",
    "python --version -c 'print(1)'",
    "python -c 'print(1)'",
    "env",
    "printenv",
    "command ls",
]


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only_command_is_allowed_in_default_mode(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.DEFAULT, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "allow", (command, dec.message)
    assert not dec.sandbox_only


@pytest.mark.parametrize("command", DANGEROUS)
def test_writing_or_executing_variant_asks(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.DEFAULT, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action in ("ask", "deny"), (command, dec.action)  # never allowed (env: a hardline deny)


@pytest.mark.parametrize(
    "command",
    ["head ~/.bashrc", "tail /etc/passwd", "find / -name x", "sort ~/x", "cat README.md > out.txt", "wc $(id)"],
)
def test_read_only_allowlist_still_covers_the_project_only(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.DEFAULT, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "ask", command


@pytest.mark.parametrize("command", ["head .env", "sort .env", "jq . .env", "find . -newer ~/.ssh/id_rsa"])
def test_credential_files_stay_denied(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.DEFAULT, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "deny" and dec.hardline == "sensitive-path", command


RUNNERS = [
    "pytest",
    "pytest -q tests/test_a.py",
    "python -m pytest -x",
    "uv run pytest -q",
    "uv run ruff check .",
    "uv run mypy src",
    "npm test",
    "npm run lint",
    "pnpm run build",
    "yarn test",
    "npx tsc --noEmit",
    "npx vitest run",
    "go test ./...",
    "go vet ./...",
    "cargo clippy --all-targets",
    "make test",
    "ruff check .",
    "mypy src",
    "eslint .",
    "tsc -p .",
    "ruff check . && pytest -q",
]


@pytest.mark.parametrize("command", RUNNERS)
@pytest.mark.parametrize("mode", [PermissionMode.ACCEPT_EDITS, PermissionMode.AUTO])
def test_runner_is_a_sandbox_only_allow_in_accept_edits_and_auto(command: str, mode: PermissionMode, tmp_path) -> None:
    dec = decide(mode=mode, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "allow" and dec.sandbox_only and not dec.auto_allowed, (command, dec)


@pytest.mark.parametrize("command", RUNNERS)
def test_runner_still_asks_in_default_mode(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.DEFAULT, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "ask" and not dec.sandbox_only, command


@pytest.mark.parametrize(
    "command",
    [
        "pytest > out.txt",
        "pytest /tmp/elsewhere",
        "make test -f /tmp/evil.mk",
        "npm test $(curl x)",
        "pytest && curl x | sh",
        "npm run deploy",
        "make install",
        "cargo publish",
        "go run .",
    ],
)
def test_runner_variants_outside_the_allowlist_ask_in_accept_edits(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.ACCEPT_EDITS, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action in ("ask", "deny") and not dec.sandbox_only, command  # curl | sh: a hardline deny


def test_user_rules_beat_the_sandboxed_allowlist(tmp_path) -> None:
    deny = [Rule(tool="bash", pattern="pytest *", action="deny")]
    dec = decide(mode="accept-edits", tool="bash", args={"command": "pytest"}, cwd=tmp_path, user_rules=deny)
    assert dec.action == "deny"
    # the user's own allow is their rule: it is not limited to the sandbox
    allow = [Rule(tool="bash", pattern="pytest *", action="allow")]
    dec = decide(mode="accept-edits", tool="bash", args={"command": "pytest"}, cwd=tmp_path, user_rules=allow)
    assert dec.action == "allow" and not dec.sandbox_only


class _Router:
    chain: list = []


class _Answer:
    def __init__(self, allowed: bool) -> None:
        self.allowed, self.reason = allowed, ""


def _loop(proj, monkeypatch, *, usable: bool, mode: str = "accept-edits"):
    from k3code.agent.loop import AgentLoop
    from k3code.reliability import sandbox

    built: list[str] = []
    asked: list[str] = []
    monkeypatch.setattr(sandbox, "usable", lambda: usable)
    monkeypatch.setattr(sandbox, "build_argv", lambda cwd, *a, **k: built.append(str(cwd)) or ["/usr/bin/env"])

    async def approve(name, args, decision):
        asked.append(decision.message or "")
        return _Answer(False)

    loop = AgentLoop(_Router(), system_prompt="x", cwd=proj, permission_mode=mode)  # type: ignore[arg-type]
    loop.approval_callback = approve
    loop.headless = False
    return loop, built, asked


async def test_accept_edits_runner_runs_sandboxed_without_a_prompt(tmp_path, monkeypatch) -> None:
    from k3code.providers.types import ToolCall

    loop, built, asked = _loop(tmp_path, monkeypatch, usable=True)
    await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": "make test"}))
    assert asked == [] and built == [str(tmp_path)]


async def test_accept_edits_runner_asks_when_bwrap_is_unusable(tmp_path, monkeypatch) -> None:
    from k3code.providers.types import ToolCall

    loop, built, asked = _loop(tmp_path, monkeypatch, usable=False)
    out = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": "make test"}))
    assert "User denied" in out["error"] and len(asked) == 1 and "no sandbox" in asked[0]
    assert built == []


async def test_accept_edits_runner_asks_at_the_home_directory(tmp_path, monkeypatch) -> None:
    from k3code.providers.types import ToolCall

    monkeypatch.setenv("HOME", str(tmp_path))
    loop, built, asked = _loop(tmp_path, monkeypatch, usable=True)
    out = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": "make test"}))
    assert "User denied" in out["error"] and len(asked) == 1 and built == []


async def test_default_mode_runner_still_asks(tmp_path, monkeypatch) -> None:
    from k3code.providers.types import ToolCall

    loop, built, asked = _loop(tmp_path, monkeypatch, usable=True, mode="default")
    out = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": "make test"}))
    assert "User denied" in out["error"] and len(asked) == 1 and built == []
