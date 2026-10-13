"""The local CI and release scripts that replaced the GitHub Actions workflows (scripts/ci, scripts/release, .githooks).

Every git repository these tests touch is a temporary one; the environment passed to the scripts has no GIT_* variables
and a fake HOME, and the GitHub CLI is a stub that records its calls.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from k3code import update as upd

REPO = Path(__file__).resolve().parents[2]
CHECK = REPO / "scripts" / "ci" / "check.sh"
MERGE_PR = REPO / "scripts" / "ci" / "merge-pr.sh"
RELEASE = REPO / "scripts" / "release" / "release.sh"
PRE_PUSH = REPO / ".githooks" / "pre-push"
INSTALL_HOOKS = REPO / "scripts" / "dev" / "install-hooks.sh"
AREAS = ["shell", "vendor", "secrets", "core", "panes", "tui"]
ZERO = "0" * 40

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash, git")


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and not k.startswith("K3CODE_")}
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    (home / ".gitconfig").write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n")
    env["HOME"] = str(home)
    env.update(extra)
    return env


def _run(args: list[str], cwd: Path, env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, timeout=300)


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    assert Path(env["HOME"]).parent in (cwd, *cwd.parents)  # only ever a repository under the test's tmp_path
    return _run(["git", *args], cwd, env).stdout.strip()


def _repo(tmp: Path, env: dict[str, str], files: dict[str, str]) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, env, "init", "-q", "-b", "Main")
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "init")
    return repo


def _stub_bin(tmp: Path, fail: tuple[str, ...] = ()) -> Path:
    """Stand-ins for every tool check.sh runs: they succeed (or fail, for the names in `fail`) and do nothing, except
    `npm ci`, which creates node_modules. gh records its arguments in gh.log."""
    bindir = tmp / "bin"
    bindir.mkdir()
    for tool in ("uv", "node", "npx", "go", "gitleaks", "shellcheck", "bwrap", "python3", "cc"):
        (bindir / tool).write_text(f"#!/bin/sh\nexit {1 if tool in fail else 0}\n")
    (bindir / "npm").write_text('#!/bin/sh\n[ "$1" = ci ] && mkdir -p node_modules\nexit 0\n')
    (bindir / "gh").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{tmp}/gh.log"\n'
        'case "$1 $2" in\n'
        '  "repo view") echo owner/repo ;;\n'
        '  "pr view") printf "%s\\n" "$FAKE_PR_TSV" ;;\n'
        "esac\n"
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    return bindir


def _gh_calls(tmp: Path) -> list[str]:
    log = tmp / "gh.log"
    return log.read_text().splitlines() if log.exists() else []


# --- check.sh ------------------------------------------------------------------------------------------------------


def test_full_plan_runs_every_former_ci_job(tmp_path: Path) -> None:
    """ci.yml ran core, tui, panes and vendor_check, gitleaks.yml the secret scan: check.sh runs the same commands."""
    out = _run(["bash", str(CHECK), "--plan"], REPO, _env(tmp_path)).stdout
    assert out.splitlines()[0] == "mode: full; areas: " + " ".join(AREAS)
    for cmd in (
        "(cd core && uv run ruff check . ../scripts)",
        "(cd core && uv run ruff format --check . ../scripts)",
        "(cd core && timeout --kill-after=30 2700 uv run pytest -q)",  # a stalled run fails (issue #42)
        "(cd tui && npm run build:ink)",
        "(cd tui && npm run typecheck)",
        "(cd tui && npm run lint)",
        "(cd tui && npx prettier --check .)",
        "(cd tui && tui_vitest)",
        "go vet ./internal/k3keys/... ./internal/harness/... ./cmd/k3/",
        "go test -race ./internal/k3keys/... ./internal/harness/...",
        "go test -timeout 30m ./internal/input/ ./internal/app/",
        "python3 scripts/vendor_check.py",
        "gitleaks detect --source . --config .gitleaks.toml",
        "--log-opts=origin/Main..HEAD",
        "(cd . && shell_lint)",
    ):
        assert cmd in out, cmd
    assert "go test ./..." not in out  # panes' upstream remote-sync tests recurse without bound


def test_quick_plan_has_no_builds_or_test_suites(tmp_path: Path) -> None:
    out = _run(["bash", str(CHECK), "--plan", "--quick"], REPO, _env(tmp_path)).stdout
    assert "uv run ruff check" in out and "npx prettier --check" in out and "go vet" in out and "shell_lint" in out
    for slow in ("pytest", "vitest", "go test", "go build", "npm run build", "typecheck"):
        assert slow not in out, slow


def test_bad_options_and_partial_post_are_refused(tmp_path: Path) -> None:
    env = _env(tmp_path)
    r = _run(["bash", str(CHECK), "--only", "nope"], REPO, env)
    assert r.returncode == 2 and "unknown area: nope" in r.stderr
    for partial in (["--quick"], ["--only", "core"], ["--changed"]):
        r = _run(["bash", str(CHECK), "--post", *partial], REPO, env)
        assert r.returncode == 2 and "--post needs the full check" in r.stderr


def test_changed_selects_the_areas_of_the_changed_files(tmp_path: Path) -> None:
    env = _env(tmp_path)
    repo = _repo(tmp_path, env, {"README.md": "x\n"})
    _git(repo, env, "update-ref", "refs/remotes/origin/Main", "HEAD")
    env["K3CODE_CI_ROOT"] = str(repo)

    def areas() -> str:
        r = _run(["bash", str(CHECK), "--plan", "--changed"], REPO, env)
        assert r.returncode == 0, r.stderr
        return r.stdout.splitlines()[0]

    assert areas() == "mode: full; areas: secrets"
    (repo / "tui").mkdir()
    (repo / "tui" / "a.ts").write_text("x\n")  # untracked
    assert areas() == "mode: full; areas: vendor secrets tui"
    (repo / "core").mkdir()
    (repo / "core" / "b.py").write_text("x\n")
    _git(repo, env, "add", "core/b.py")
    _git(repo, env, "commit", "-q", "-m", "core")  # committed on the branch, not on origin/Main
    assert areas() == "mode: full; areas: vendor secrets core tui"
    (repo / "install").mkdir()
    (repo / "install" / "x.sh").write_text("true\n")
    assert areas() == "mode: full; areas: shell vendor secrets core tui"


def _ci_repo(tmp: Path, env: dict[str, str]) -> Path:
    repo = _repo(
        tmp,
        env,
        {
            ".gitignore": "node_modules/\n",
            "tui/package-lock.json": "{}\n",
            "core/k": "",
            "panes/k": "",
            "README.md": "x\n",
        },
    )
    _git(repo, env, "update-ref", "refs/remotes/origin/Main", "HEAD")
    return repo


def test_post_sets_the_local_ci_status_after_a_full_run(tmp_path: Path) -> None:
    env = _env(tmp_path)
    repo = _ci_repo(tmp_path, env)
    env.update(PATH=f"{_stub_bin(tmp_path)}:{env['PATH']}", K3CODE_CI_ROOT=str(repo))
    env["K3CODE_CI_LOG_DIR"] = str(tmp_path / "logs")
    r = _run(["bash", str(CHECK), "--post"], REPO, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "local-ci: PASS" in r.stdout
    sha = _git(repo, env, "rev-parse", "HEAD")
    api = [c for c in _gh_calls(tmp_path) if c.startswith("api")]
    assert len(api) == 1
    assert f"repos/owner/repo/statuses/{sha}" in api[0]
    assert "state=success" in api[0] and "context=local-ci" in api[0]
    assert "description=shell,vendor,secrets,core,panes,tui passed on" in api[0]
    assert (tmp_path / "logs" / "summary.txt").is_file()


def test_post_reports_a_failed_area_as_failure(tmp_path: Path) -> None:
    env = _env(tmp_path)
    repo = _ci_repo(tmp_path, env)
    env.update(PATH=f"{_stub_bin(tmp_path, fail=('go',))}:{env['PATH']}", K3CODE_CI_ROOT=str(repo))
    env["K3CODE_CI_LOG_DIR"] = str(tmp_path / "logs")
    r = _run(["bash", str(CHECK), "--post"], REPO, env)
    assert r.returncode == 1
    assert "local-ci: FAIL: panes" in r.stdout
    api = [c for c in _gh_calls(tmp_path) if c.startswith("api")]
    assert len(api) == 1 and "state=failure" in api[0] and "description=failed: panes on" in api[0]


def test_post_refuses_a_dirty_tree_or_a_commit_not_on_origin(tmp_path: Path) -> None:
    env = _env(tmp_path)
    repo = _ci_repo(tmp_path, env)
    env.update(PATH=f"{_stub_bin(tmp_path)}:{env['PATH']}", K3CODE_CI_ROOT=str(repo))
    env["K3CODE_CI_LOG_DIR"] = str(tmp_path / "logs")
    (repo / "README.md").write_text("changed\n")
    r = _run(["bash", str(CHECK), "--post"], REPO, env)
    assert r.returncode == 2 and "uncommitted or untracked" in r.stderr
    _git(repo, env, "commit", "-q", "-am", "local only")
    r = _run(["bash", str(CHECK), "--post"], REPO, env)
    assert r.returncode == 2 and "is on no origin branch" in r.stderr
    assert _gh_calls(tmp_path) == []  # nothing ran, nothing was posted
    assert not (tmp_path / "logs").exists()


def test_a_failed_npm_ci_fails_tui_and_leaves_no_stamp(tmp_path: Path) -> None:
    env = _env(tmp_path)
    repo = _ci_repo(tmp_path, env)
    bindir = _stub_bin(tmp_path)
    # a partial install: node_modules exists, but npm ci failed
    (bindir / "npm").write_text('#!/bin/sh\n[ "$1" = ci ] && { mkdir -p node_modules/partial; exit 1; }\nexit 0\n')
    env.update(PATH=f"{bindir}:{env['PATH']}", K3CODE_CI_ROOT=str(repo), K3CODE_CI_LOG_DIR=str(tmp_path / "logs"))
    for _ in range(2):  # the second run must retry npm ci, not trust the broken node_modules
        r = _run(["bash", str(CHECK), "--only", "tui", "--quick"], REPO, env)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "FAILED: npm ci (exit 1)" in r.stderr
        assert "npm ci skipped" not in (tmp_path / "logs" / "tui.log").read_text()
        assert not (repo / "tui" / "node_modules" / ".k3ci-lock").exists()


def test_a_missing_tool_fails_its_area_with_an_install_hint(tmp_path: Path) -> None:
    env = _env(tmp_path)
    repo = _ci_repo(tmp_path, env)
    bindir = _stub_bin(tmp_path)
    (bindir / "shellcheck").unlink()
    # PATH: the stubs plus the system tools check.sh itself needs, without any real shellcheck
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    for tool in (
        "bash",
        "git",
        "nice",
        "date",
        "find",
        "sort",
        "head",
        "tail",
        "tee",
        "sed",
        "cat",
        "mkdir",
        "dirname",
    ):
        src = shutil.which(tool)
        assert src, tool
        (sysbin / tool).symlink_to(src)
    env.update(PATH=f"{bindir}:{sysbin}", K3CODE_CI_ROOT=str(repo), K3CODE_CI_LOG_DIR=str(tmp_path / "logs"))
    r = _run([shutil.which("bash") or "bash", str(CHECK), "--only", "shell"], REPO, env)
    assert r.returncode == 1
    assert "shellcheck is not installed: install shellcheck:" in r.stderr
    assert "missing shellcheck" in r.stdout


# --- hooks ---------------------------------------------------------------------------------------------------------


def _hook_repo(tmp: Path, env: dict[str, str]) -> Path:
    stub = '#!/bin/sh\nprintf "%s\\n" "check $*" >> "$(dirname "$0")/../../check.log"\n'
    repo = _repo(tmp, env, {"scripts/ci/check.sh": stub, "README.md": "x\n"})
    return repo


@pytest.mark.parametrize(
    ("remote_ref", "expect"),
    [
        ("refs/heads/feat/x", "check --quick"),
        ("refs/heads/Main", "check "),
        ("refs/tags/v1.0.0", "check "),
    ],
)
def test_pre_push_runs_the_full_check_for_main_and_tags(tmp_path: Path, remote_ref: str, expect: str) -> None:
    env = _env(tmp_path)
    repo = _hook_repo(tmp_path, env)
    sha = _git(repo, env, "rev-parse", "HEAD")
    r = _run(["bash", str(PRE_PUSH), "origin", "url"], repo, env, stdin=f"refs/heads/x {sha} {remote_ref} {ZERO}\n")
    assert r.returncode == 0, r.stderr
    assert (repo / "check.log").read_text() == expect + "\n"


def test_pre_push_full_wins_when_main_is_one_of_several_refs(tmp_path: Path) -> None:
    env = _env(tmp_path)
    repo = _hook_repo(tmp_path, env)
    sha = _git(repo, env, "rev-parse", "HEAD")
    lines = f"refs/heads/a {sha} refs/heads/a {ZERO}\nrefs/heads/Main {sha} refs/heads/Main {ZERO}\n"
    _run(["bash", str(PRE_PUSH), "origin", "url"], repo, env, stdin=lines)
    assert (repo / "check.log").read_text() == "check \n"


def test_pre_push_skips_deletions_and_honours_k3code_skip_hooks(tmp_path: Path) -> None:
    env = _env(tmp_path)
    repo = _hook_repo(tmp_path, env)
    sha = _git(repo, env, "rev-parse", "HEAD")
    r = _run(["bash", str(PRE_PUSH), "origin", "url"], repo, env, stdin=f"(delete) {ZERO} refs/heads/old {sha}\n")
    assert r.returncode == 0 and not (repo / "check.log").exists()
    env["K3CODE_SKIP_HOOKS"] = "1"
    line = f"refs/heads/Main {sha} refs/heads/Main {ZERO}\n"
    r = _run(["bash", str(PRE_PUSH), "origin", "url"], repo, env, stdin=line)
    assert r.returncode == 0 and not (repo / "check.log").exists()
    assert "SKIPPED" in r.stderr


def test_install_hooks_sets_a_repo_local_hooks_path_idempotently(tmp_path: Path) -> None:
    env = _env(tmp_path)
    repo = _repo(tmp_path, env, {".githooks/pre-push": "#!/bin/sh\n"})
    r = _run(["sh", str(INSTALL_HOOKS)], repo, env)
    assert r.returncode == 0, r.stderr
    assert _git(repo, env, "config", "--local", "core.hooksPath") == ".githooks"
    assert "already installed" in _run(["sh", str(INSTALL_HOOKS)], repo, env).stdout
    assert not (Path(env["HOME"]) / ".gitconfig").read_text().count("hooksPath")  # never the global config
    _run(["sh", str(INSTALL_HOOKS), "--uninstall"], repo, env)
    assert _git(repo, env, "config", "--local", "core.hooksPath") == ""


# --- merge-pr.sh ---------------------------------------------------------------------------------------------------


def _pr_setup(tmp: Path) -> tuple[Path, dict[str, str], str]:
    """A bare origin with Main and a PR branch (one commit each side, so origin/Main must be merged in), a clone of it,
    and stub tools on PATH."""
    env = _env(tmp)
    origin = tmp / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", "Main", str(origin)], tmp, env)
    repo = _repo(
        tmp,
        env,
        {".gitignore": "node_modules/\n.k3dev/\n", "tui/package-lock.json": "{}\n", "core/k": "", "panes/k": ""},
    )
    _git(repo, env, "remote", "add", "origin", str(origin))
    _git(repo, env, "push", "-q", "origin", "Main")
    _git(repo, env, "switch", "-q", "-c", "feat/x")
    (repo / "feature.txt").write_text("f\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "feature")
    _git(repo, env, "push", "-q", "origin", "feat/x")
    head = _git(repo, env, "rev-parse", "HEAD")
    _git(repo, env, "switch", "-q", "Main")
    (repo / "main.txt").write_text("m\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "main moved")
    _git(repo, env, "push", "-q", "origin", "Main")
    env["PATH"] = f"{_stub_bin(tmp)}:{env['PATH']}"
    return repo, env, head


def _pr_tsv(head: str, *, state: str = "OPEN", base: str = "Main", cross: str = "false", draft: str = "true") -> str:
    return "\t".join([state, base, "feat/x", head, cross, draft, "https://example.invalid/pr/7"])


@pytest.mark.parametrize(
    ("kw", "message"),
    [({"state": "CLOSED"}, "is CLOSED, not open"), ({"base": "dev"}, "based on dev"), ({"cross": "true"}, "fork")],
)
def test_merge_pr_refuses_a_pr_it_cannot_merge(tmp_path: Path, kw: dict[str, str], message: str) -> None:
    repo, env, head = _pr_setup(tmp_path)
    env["FAKE_PR_TSV"] = _pr_tsv(head, **kw)
    r = _run(["bash", str(MERGE_PR), "7"], repo, env)
    assert r.returncode == 2 and message in r.stderr


def test_merge_pr_dry_run_merges_main_checks_and_pushes_nothing(tmp_path: Path) -> None:
    repo, env, head = _pr_setup(tmp_path)
    env["FAKE_PR_TSV"] = _pr_tsv(head)
    r = _run(["bash", str(MERGE_PR), "--dry-run", "7"], repo, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "merged origin/Main into feat/x" in r.stdout and "local-ci: PASS" in r.stdout
    assert "gh pr ready 7" in r.stdout and "gh pr merge 7 --merge --match-head-commit" in r.stdout
    assert _git(repo, env, "ls-remote", "origin", "refs/heads/feat/x").split()[0] == head  # nothing pushed
    assert [c for c in _gh_calls(tmp_path) if not c.startswith("pr view")] == []
    assert len(_git(repo, env, "worktree", "list").splitlines()) == 1  # the temporary worktree is gone


def test_merge_pr_pushes_the_checked_merge_posts_and_merges(tmp_path: Path) -> None:
    repo, env, head = _pr_setup(tmp_path)
    env["FAKE_PR_TSV"] = _pr_tsv(head)
    r = _run(["bash", str(MERGE_PR), "7"], repo, env)
    assert r.returncode == 0, r.stdout + r.stderr
    merged = _git(repo, env, "ls-remote", "origin", "refs/heads/feat/x").split()[0]
    assert merged != head
    assert _git(repo, env, "rev-parse", f"{merged}^1") == head  # a fast-forward of the PR branch
    calls = [c for c in _gh_calls(tmp_path) if not c.startswith(("pr view", "repo view"))]
    assert calls[0].startswith(f"api --silent repos/owner/repo/statuses/{merged} ")
    assert "state=success" in calls[0] and "context=local-ci" in calls[0]
    assert calls[1:] == ["pr ready 7", f"pr merge 7 --merge --match-head-commit {merged}"]


def test_merge_pr_stops_when_the_check_fails(tmp_path: Path) -> None:
    repo, env, head = _pr_setup(tmp_path)
    env["FAKE_PR_TSV"] = _pr_tsv(head)
    (tmp_path / "bin" / "uv").write_text("#!/bin/sh\nexit 1\n")
    r = _run(["bash", str(MERGE_PR), "7"], repo, env)
    assert r.returncode == 2 and "the full check failed" in r.stderr and "Logs: " in r.stderr
    assert _git(repo, env, "ls-remote", "origin", "refs/heads/feat/x").split()[0] == head
    assert [c for c in _gh_calls(tmp_path) if not c.startswith("pr view")] == []


# --- release.sh and the updater ------------------------------------------------------------------------------------


def _assets(ver: str) -> list[str]:
    r = subprocess.run(["bash", str(RELEASE), "--list-assets", ver], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return [n.replace("*", ver) for n in r.stdout.split()]  # the wheel is listed as a pattern


def test_release_ships_what_release_yml_shipped() -> None:
    names = _assets("1.2.3")
    assert names == [
        "k3code-1.2.3-py3-none-any.whl",
        "k3code-1.2.3-requirements.txt",
        "k3code-tui-1.2.3.tar.gz",
        "k3-linux-amd64",
        "k3-linux-arm64",
        "k3-darwin-amd64",
        "k3-darwin-arm64",
        "SHA256SUMS",
    ]
    text = RELEASE.read_text()
    for needle in (
        "uv build --wheel",
        "CGO_ENABLED=0",
        "uv export --quiet --locked",
        "gh release create",
        "--prerelease",
    ):
        assert needle in text


@pytest.mark.parametrize(("plat", "machine", "arch"), [("linux", "x86_64", "amd64"), ("darwin", "arm64", "arm64")])
def test_update_installs_the_assets_release_sh_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plat: str, machine: str, arch: str
) -> None:
    """k3code update (update.install_release) finds the wheel, the TUI and this platform's k3 among the release's
    assets by name, and verifies them against SHA256SUMS."""
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    ver = "1.2.3"
    names = _assets(ver)
    assert any(n.endswith(getattr(upd, "REQUIREMENTS_SUFFIX", "-requirements.txt")) for n in names)
    tui = io.BytesIO()
    with tarfile.open(fileobj=tui, mode="w:gz") as t:
        info = tarfile.TarInfo("dist/entry.js")
        info.size = 2
        t.addfile(info, io.BytesIO(b"//"))
    content = {n: (tui.getvalue() if n.startswith("k3code-tui-") else n.encode()) for n in names if n != "SHA256SUMS"}
    content["SHA256SUMS"] = "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in content.items()).encode()
    monkeypatch.setattr(upd, "_download", lambda url, dest, token: dest.write_bytes(content[url]))
    calls: list[list[str]] = []
    monkeypatch.setattr(upd.subprocess, "run", lambda cmd, **kw: calls.append([str(c) for c in cmd]))
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    rel = upd.Release(tag=f"v{ver}", version=ver, body="", prerelease=False, assets={n: n for n in names})
    vdir = upd.install_release(rel, None, uv="uv")
    assert any(c[-1].endswith(f"k3code-{ver}-py3-none-any.whl") for c in calls if "install" in c)
    assert (vdir / "tui" / "dist" / "entry.js").read_text() == "//"
    assert (vdir / "bin" / "k3").read_bytes() == f"k3-{plat}-{arch}".encode()
    assert (vdir / ".complete").is_file()


def test_update_rejects_an_asset_that_does_not_match_sha256sums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    names = _assets("1.2.3")
    sums = "".join(f"{hashlib.sha256(n.encode()).hexdigest()}  {n}\n" for n in names if n != "SHA256SUMS")
    content = {n: n.encode() for n in names} | {"SHA256SUMS": sums.encode(), "k3-linux-amd64": b"tampered"}
    monkeypatch.setattr(upd, "_download", lambda url, dest, token: dest.write_bytes(content[url]))
    rel = upd.Release(tag="v1.2.3", version="1.2.3", body="", prerelease=False, assets={n: n for n in names})
    with pytest.raises(ValueError, match="k3-linux-amd64"):
        upd.install_release(rel, None, uv="uv")


# --- release.sh: a failed publish leaves no tag behind -------------------------------------------------------------

_RELEASE_TOOLS = {
    "uv": """#!/bin/sh
case "$1" in
  build) while [ $# -gt 0 ]; do [ "$1" = --out-dir ] && out=$2; shift; done
         mkdir -p "$out" && echo wheel > "$out/k3code-1.2.3-py3-none-any.whl" ;;
  export) while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done; echo requirements > "$out" ;;
esac
""",
    "npm": "#!/bin/sh\nmkdir -p dist\n",
    "go": '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done; echo k3 > "$out"\n',
    # The part of GitHub release.sh depends on: a draft release has no tag, publishing it creates the tag in the origin
    # at the draft's target. State: $FAKE_GH_DIR/draft-<tag> (the target, then one asset name per line).
    # `release list` prints what the script's --jq leaves of the list: the tags of the drafts. `release view` leaves
    # out the asset named by FAKE_GH_HIDE (an upload that did not arrive).
    "gh": """#!/bin/sh
d=$FAKE_GH_DIR
printf '%s\\n' "$*" >> "$d/gh.log"
tag=$3
publish() {
  git --git-dir "$FAKE_GH_ORIGIN" rev-parse -q --verify "refs/tags/$tag" >/dev/null ||
    git --git-dir "$FAKE_GH_ORIGIN" tag "$tag" "$(head -n 1 "$d/draft-$tag")"
  rm "$d/draft-$tag"
}
case "$1 $2" in
  "release list") for f in "$d"/draft-*; do [ -f "$f" ] && basename "$f" | sed 's/^draft-//'; done; exit 0 ;;
  "release view") tail -n +2 "$d/draft-$tag" | grep -v -x -e "${FAKE_GH_HIDE:-}" || exit 1 ;;
  "release delete") rm "$d/draft-$tag" || exit 1 ;;
  "release edit") [ "$FAKE_GH_FAIL" = edit ] && exit 1; publish ;;
  "release create")
    [ -f "$d/draft-$tag" ] && { echo "release $tag already exists" >&2; exit 1; }
    shift 3; draft=0; : > "$d/new"; : > "$d/target"
    while [ $# -gt 0 ]; do
      case $1 in
        --target) echo "$2" > "$d/target"; shift ;;
        --title | --notes-file) shift ;;
        --draft) draft=1 ;;
        -*) ;;
        *) [ -f "$1" ] && basename "$1" >> "$d/new" ;;
      esac
      shift
    done
    { cat "$d/target"; cat "$d/new"; } > "$d/draft-$tag"; rm "$d/new" "$d/target"
    [ "$FAKE_GH_FAIL" = create ] && exit 1  # an upload that fails leaves the draft behind
    [ "$draft" = 1 ] || publish ;;
esac
exit 0
""",
}


def _release_setup(tmp: Path) -> tuple[Path, dict[str, str], Path]:
    """A copy of the release scripts in a repository on Main with a bare origin, and stand-ins for uv, npm, go, gh."""
    env = _env(tmp)
    origin = tmp / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", "Main", str(origin)], tmp, env)
    files = {
        ".gitignore": "node_modules/\n.k3dev/\ntui/dist/\n",
        "VERSION": "1.2.3\n",
        "CHANGELOG.md": "# Changelog\n\n## [1.2.3] - 2026-01-01\n\n- a change\n",
        "core/k": "",
        "tui/k": "",
        "panes/k": "",
        "scripts/ci/check.sh": "#!/bin/sh\nexit 0\n",
        "scripts/release/build_release.sh": "#!/bin/sh\nexit 0\n",
    }
    for rel in ("scripts/ci/lib.sh", "scripts/release/release.sh"):
        files[rel] = (REPO / rel).read_text()
    repo = _repo(tmp, env, files)
    _git(repo, env, "remote", "add", "origin", str(origin))
    _git(repo, env, "push", "-q", "origin", "Main")
    bindir = _stub_bin(tmp)
    for tool, text in _RELEASE_TOOLS.items():
        (bindir / tool).write_text(text)
        (bindir / tool).chmod(0o755)
    (tmp / "gh-state").mkdir()
    env.update(PATH=f"{bindir}:{env['PATH']}", FAKE_GH_DIR=str(tmp / "gh-state"), FAKE_GH_ORIGIN=str(origin))
    return repo, env, origin


def _release_calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_GH_DIR"]) / "gh.log"
    return log.read_text().splitlines() if log.exists() else []


def _remote_tags(env: dict[str, str], origin: Path) -> list[str]:
    return _git(origin, env, "tag", "--list").split()


def test_release_creates_the_tag_only_by_publishing_a_complete_draft(tmp_path: Path) -> None:
    repo, env, origin = _release_setup(tmp_path)
    r = _run(["bash", str(repo / "scripts/release/release.sh"), "1.2.3"], repo, env)
    assert r.returncode == 0, r.stdout + r.stderr
    sha = _git(repo, env, "rev-parse", "HEAD")
    calls = [c for c in _release_calls(env) if c.startswith("release")]
    assert [c.split()[1] for c in calls] == ["list", "create", "view", "edit"]
    create = calls[1].split()
    assert "--draft" in create and create[create.index("--target") + 1] == sha  # the tag will name the checked commit
    assert "--verify-tag" not in create  # there is no tag yet to verify
    assert calls[3] == "release edit v1.2.3 --draft=false"
    assert _remote_tags(env, origin) == ["v1.2.3"]  # created by publishing...
    assert _git(origin, env, "rev-parse", "v1.2.3") == sha
    assert _git(repo, env, "tag", "--list") == ""  # ...not pushed by the script


@pytest.mark.parametrize("failing_step", ["create", "edit"])
def test_release_failing_before_the_publish_leaves_no_tag_and_can_be_rerun(tmp_path: Path, failing_step: str) -> None:
    """A tag that exists blocks a rerun (release.sh refuses an existing tag) and is what install.sh takes as the
    newest version. A gh failure must leave neither."""
    repo, env, origin = _release_setup(tmp_path)
    script = str(repo / "scripts/release/release.sh")
    r = _run(["bash", script, "1.2.3"], repo, {**env, "FAKE_GH_FAIL": failing_step})
    assert r.returncode != 0
    assert _remote_tags(env, origin) == [] and _git(repo, env, "tag", "--list") == ""
    assert _git(repo, env, "ls-remote", "--tags", "origin") == ""
    assert (Path(env["FAKE_GH_DIR"]) / "draft-v1.2.3").is_file()  # the half-made release is still a draft
    r = _run(["bash", script, "1.2.3"], repo, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "replacing the draft release v1.2.3" in r.stderr
    assert "release delete v1.2.3 --yes" in _release_calls(env)  # the draft only: never --cleanup-tag
    assert _remote_tags(env, origin) == ["v1.2.3"]


def test_release_refuses_a_draft_that_lacks_an_asset(tmp_path: Path) -> None:
    repo, env, origin = _release_setup(tmp_path)
    r = _run(["bash", str(repo / "scripts/release/release.sh"), "1.2.3"], repo, {**env, "FAKE_GH_HIDE": "SHA256SUMS"})
    assert r.returncode == 2 and "stays a draft" in r.stderr
    assert not any(c.startswith("release edit") for c in _release_calls(env))
    assert _remote_tags(env, origin) == []


def test_release_dry_run_describes_the_draft_flow_and_contacts_no_release(tmp_path: Path) -> None:
    repo, env, _ = _release_setup(tmp_path)
    r = _run(["bash", str(repo / "scripts/release/release.sh"), "--dry-run", "--no-check", "1.2.3"], repo, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--draft --target" in r.stdout and "release edit v1.2.3 --draft=false" in r.stdout
    assert "git push" not in r.stdout and "git tag" not in r.stdout
    assert [c for c in _release_calls(env) if c.startswith("release")] == []
