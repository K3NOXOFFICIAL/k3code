"""accept-edits reads outside the project roots without a prompt only what holds no credentials: a dot folder, a
credential-like name, the system's runtime trees and other users' homes still ask a person, and a grep or glob of an
allowed directory outside the roots withholds the same files below it, in the rg walk and in the Python fallback."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from k3code.permissions import decide
from k3code.tools import tool_glob, tool_grep

SECRET = "SECRETTOKEN"
MARKER = "MARKERWORD"
#: Files below ~/projects/other whose text or name must not leave a search of a directory outside the roots.
SECRET_FILES = (
    ".npmrc",
    ".git-credentials",
    ".aws/credentials",
    ".git/config",
    ".config/tool/token.json",
    "keys/server.pem",
    "keys/id_ed25519",
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(fake $HOME, a project directory outside it that is the session's cwd)."""
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    home = tmp_path / "home"
    other = home / "projects" / "other"
    for rel in SECRET_FILES:
        (other / rel).parent.mkdir(parents=True, exist_ok=True)
        (other / rel).write_text(f"url = https://u:{SECRET}@example.com/x\n_authToken={SECRET}\n")
    (other / "notes.txt").write_text(f"an ordinary {MARKER} line\n")
    (other / ".github").mkdir()
    (other / ".github" / "ci.yml").write_text(f"name: {MARKER} ci\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return home, cwd


def _rg_or_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fallback: bool) -> None:
    if fallback:
        monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))  # no rg (and no git): the Python fallback walker
    elif not shutil.which("rg"):
        pytest.skip("rg not installed")


def _assert_clean(text: str) -> None:
    assert SECRET not in text, text
    for rel in SECRET_FILES:
        assert rel not in text, (rel, text)
    for name in ("npmrc", "git-credentials", "credentials", ".git/", "server.pem", "id_ed25519", "token.json"):
        assert name not in text, (name, text)


@pytest.mark.parametrize("fallback", [True, False], ids=["fallback", "rg"])
@pytest.mark.parametrize("sub", ["projects", "projects/other"])
async def test_a_search_outside_the_roots_returns_no_credential_file(world, tmp_path, monkeypatch, fallback, sub):
    home, cwd = world
    searched = str(home / sub)
    for tool in ("grep", "glob"):
        d = decide(mode="accept-edits", tool=tool, args={"path": searched, "pattern": "*"}, cwd=str(cwd))
        assert d.action == "allow", (tool, d)
    _rg_or_fallback(monkeypatch, tmp_path, fallback)
    # an include glob of "*" turns rg's hidden-file filter off; the fallback walks hidden files anyway
    for include in (None, "*"):
        args = {"pattern": f"{SECRET}|{MARKER}", "path": searched, **({"include": include} if include else {})}
        res = await tool_grep(args, cwd=cwd)
        assert "error" not in res, res
        _assert_clean(res["matches"])
        assert f"notes.txt:1:an ordinary {MARKER} line" in res["matches"], res
    for pattern in ("*", "**/.npmrc", "**/credentials", "**/.aws", "**/.git*"):
        res = await tool_glob({"pattern": pattern, "path": searched}, cwd=cwd)
        _assert_clean("\n".join(res["files"]))
    listed = (await tool_glob({"pattern": "*", "path": searched}, cwd=cwd))["files"]
    assert any(f.endswith("notes.txt") for f in listed), listed
    assert any(f.endswith(".github/ci.yml") for f in listed), listed  # a benign dot folder stays visible


@pytest.mark.parametrize(
    "path",
    [
        "/run/secrets/x",
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
        "/run/user/1000/containers/auth.json",
        "/tmp/krb5cc_1000",
        "/proc/self/cmdline",
        "/proc/self/maps",
        "/proc/self/fd/0",
        "/proc/1/cwd/x",
        "/sys/kernel/x",
        "/boot/x",
        "/home/bob/.ssh/id_rsa",
        "/home/bob/.aws/credentials",
        "/home/bob/notes.txt",
        "/root/.ssh/id_rsa",
        "/root/notes.txt",
        "/mnt/c/Users/x/.git-credentials",
        "/mnt/c/Users/x/.azure/x",
        "/mnt/c/Users/x/.kube/config",
        "/mnt/c/Users/x/.docker/config.json",
        "{sib}/.npmrc",
        "{sib}/.git-credentials",
        "{sib}/.aws/credentials",
        "{sib}/.git/config",
        "{sib}/.GIT/config",
        "{sib}/deploy.pem",
        "{sib}/keys/id_rsa.bak",
        "{sib}/credentials.json",
        "{sib}/gcloud-credentials.json",
        "{sib}/kubeconfig",
        "{sib}/kubeconfig-prod.yaml",
        "{sib}/terraform.tfstate",
        "{sib}/terraform.tfstate.backup",
        "{sib}/token",
        "{sib}/token.json",
        "{sib}/secrets.yaml",
        "{sib}/secret.json",
        "{sib}/service-account.json",
        "{sib}/service_account_key.json",
        "{sib}/cert.p12",
        "{sib}/store.jks",
        "{sib}/vault.kdbx",
        "{sib}/putty.ppk",
    ],
)
def test_accept_edits_still_asks_for_credential_places_outside_the_roots(world, tmp_path, path) -> None:
    _, cwd = world
    path = path.format(sib=tmp_path / "sibling")
    for tool in ("read", "grep", "glob"):
        d = decide(mode="accept-edits", tool=tool, args={"path": path, "pattern": "*"}, cwd=str(cwd))
        assert d.action == "ask" and d.needs_human, (tool, path, d)


def test_a_link_into_a_dot_folder_asks_by_its_target(world, tmp_path) -> None:
    _, cwd = world
    sib = tmp_path / "sibling"
    (sib / ".hidden").mkdir(parents=True)
    (sib / ".hidden" / "token").write_text("x\n")
    (sib / "plain").symlink_to(sib / ".hidden" / "token")
    d = decide(mode="accept-edits", tool="read", args={"path": str(sib / "plain")}, cwd=str(cwd))
    assert d.action == "ask" and d.needs_human, d


@pytest.mark.parametrize(
    "rel",
    [
        "notes.txt",
        "src/app.py",
        ".github/workflows/ci.yml",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".vscode/settings.json",
        ".prettierrc.json",
        ".eslintrc.cjs",
        ".nvmrc",
        ".python-version",
        ".tool-versions",
        ".dockerignore",
        ".env.example",
        "tokenizer.py",
        "token_utils.py",
        "secrets_manager.py",
        "docs/secret-handling.md",
        "src/service_account_model.py",
    ],
)
def test_accept_edits_reads_ordinary_files_outside_the_roots(world, tmp_path, rel) -> None:
    home, cwd = world
    paths = [str(tmp_path / "sibling" / rel)]
    if not rel.startswith("."):  # below $HOME any dot folder keeps asking (_home_secret_spelled), benign or not
        paths.append(str(home / "projects" / "other" / rel))
    for path in paths:
        d = decide(mode="accept-edits", tool="read", args={"path": path}, cwd=str(cwd))
        assert d.action == "allow", (path, d)
