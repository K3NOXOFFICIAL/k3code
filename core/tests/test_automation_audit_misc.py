"""Audit findings outside the automation package: the updater, the service installer, the session index."""

from __future__ import annotations

import contextlib
import hashlib
import subprocess
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from k3code import service
from k3code import update as upd
from k3code.cli import cli
from k3code.gateway.sessions import SessionStore

WHEEL = "k3code-9.0.0-py3-none-any.whl"
REQS = "k3code-9.0.0-requirements.txt"


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    return tmp_path / "data"


# ── 6: /update from the daemon runs against the daemon's environment ──────────


def test_detached_update_carries_the_install_tree_and_api_but_never_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    monkeypatch.setenv("K3CODE_DATA", "/srv/k3/data")
    monkeypatch.setenv("K3CODE_HOME", "/srv/k3/home")
    monkeypatch.setenv("K3CODE_UPDATE_API", "https://mirror.example/api")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_TOPSECRET")
    monkeypatch.setenv("K3CODE_API_KEY", "sk-k3-TOPSECRET")  # a K3CODE_* name, but a provider key
    monkeypatch.setattr(upd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(upd.subprocess, "run", lambda argv, **k: seen.append(list(argv)) or R())
    upd.apply_detached(unit_installed=lambda: True)
    argv = seen[0]
    assert argv[:3] == ["/usr/bin/systemd-run", "--user", "--collect"] and argv[-3:] == [
        "/usr/bin/k3code",
        "update",
        "--yes",
    ]
    setenv = sorted(a for a in argv if a.startswith("--setenv="))
    assert setenv == [
        "--setenv=K3CODE_DATA=/srv/k3/data",
        "--setenv=K3CODE_HOME=/srv/k3/home",
        "--setenv=K3CODE_UPDATE_API=https://mirror.example/api",
    ]
    # GITHUB_TOKEN and the other keys come from the file the daemon's own unit reads
    assert f"--property=EnvironmentFile=-{tmp_path / 'me' / '.config' / 'k3code' / 'env'}" in argv
    assert "TOPSECRET" not in " ".join(argv)


def test_a_mirror_url_with_credentials_is_not_put_on_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("K3CODE_UPDATE_API", "https://user:pw@mirror.example/api")
    assert not any("pw@" in a for a in upd._transient_env())


# ── 11: the updater downloads what it installs and sends the token only to GitHub ─


def _fake_uv(tmp_path: Path, script: str = "") -> str:
    exe = tmp_path / "bin" / "uv"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f'#!/bin/sh\n{script}\nif [ "$1" = venv ]; then mkdir -p "$4/bin"; fi\n')
    exe.chmod(0o755)
    return str(exe)


def _release(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> tuple[upd.Release, list[str]]:
    content = {n: n.encode() for n in names}
    sums = "".join(f"{hashlib.sha256(content[n]).hexdigest()}  {n}\n" for n in names)
    by_url = {**{f"u/{n}": content[n] for n in names}, "u/SHA256SUMS": sums.encode()}
    downloaded: list[str] = []
    monkeypatch.setattr(
        upd, "_download", lambda url, dest, tok: downloaded.append(url) or dest.write_bytes(by_url[url])
    )
    assets = {n: f"u/{n}" for n in [*names, "SHA256SUMS"]}
    return upd.Release(tag="v9.0.0", version="9.0.0", body="", prerelease=False, assets=assets), downloaded


def test_only_this_platforms_binary_is_downloaded(data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    platforms = ["k3-linux-amd64", "k3-linux-arm64", "k3-darwin-amd64", "k3-darwin-arm64"]
    rel, downloaded = _release(monkeypatch, [WHEEL, REQS, "k3code-tui-9.0.0.tar.gz", *platforms])
    monkeypatch.setattr(upd, "platform_binary", lambda: "k3-linux-arm64")
    # the tui tarball is not a real tarball here: leave it out of the build, only the download list matters
    rel.assets.pop("k3code-tui-9.0.0.tar.gz")
    downloaded.clear()
    vdir = upd.install_release(rel, None, uv=_fake_uv(tmp_path))
    assert sorted(u.removeprefix("u/") for u in downloaded) == sorted([WHEEL, REQS, "SHA256SUMS", "k3-linux-arm64"])
    assert (vdir / "bin" / "k3").read_bytes() == b"k3-linux-arm64"


def test_platform_binary_names_this_machine() -> None:
    import platform
    import sys

    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), platform.machine())
    assert upd.platform_binary() == f"k3-{sys.platform}-{arch}"


def test_the_token_goes_to_the_github_api_host_only() -> None:
    assert upd._headers("tok", url=f"{upd.DEFAULT_API}/repos/o/r/releases")["Authorization"] == "Bearer tok"
    assert "Authorization" not in upd._headers("tok", url="https://mirror.example/repos/o/r/releases")
    assert "Authorization" not in upd._headers("tok", url="http://api.github.com/x")  # not over plain http
    assert "Authorization" not in upd._headers("tok", url="https://api.github.com.evil.example/x")
    assert "Authorization" not in upd._headers(None, url=upd.DEFAULT_API)


def _fake_get(monkeypatch: pytest.MonkeyPatch, resp: httpx.Response) -> list[dict]:
    calls: list[dict] = []

    def fake(url: str, headers: dict, timeout: float) -> httpx.Response:
        calls.append({"url": url, "headers": headers})
        return resp

    monkeypatch.setattr(upd.httpx, "get", fake)
    return calls


def test_fetch_latest_withholds_the_token_from_a_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_get(monkeypatch, httpx.Response(200, json=[], request=httpx.Request("GET", "https://x")))
    assert upd.fetch_latest("stable", "o/r", "tok", "https://mirror.example/api") is None
    assert upd.fetch_latest("stable", "o/r", "tok", None) is None
    assert "Authorization" not in calls[0]["headers"] and calls[1]["headers"]["Authorization"] == "Bearer tok"
    assert calls[0]["url"].startswith("https://mirror.example/api/repos/o/r/releases")


def test_download_withholds_the_token_from_a_url_a_mirror_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[dict] = []

    class Resp:
        def raise_for_status(self) -> None: ...
        def iter_bytes(self):
            yield b"x"

    @contextlib.contextmanager
    def fake_stream(method, url, headers, **kw):
        sent.append({"url": url, "headers": headers})
        yield Resp()

    monkeypatch.setattr(upd.httpx, "stream", fake_stream)
    upd._download("https://evil.example/asset/1", tmp_path / "a", "tok")
    upd._download("https://api.github.com/repos/o/r/releases/assets/1", tmp_path / "b", "tok")
    assert "Authorization" not in sent[0]["headers"] and sent[1]["headers"]["Authorization"] == "Bearer tok"


def test_a_404_from_a_mirror_says_the_token_was_not_sent_there(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_get(monkeypatch, httpx.Response(404, json={}, request=httpx.Request("GET", "https://x")))
    with pytest.raises(PermissionError, match=r"only sent to api\.github\.com, not to mirror\.example"):
        upd.fetch_latest("stable", "o/r", "tok", "https://mirror.example/api")
    with pytest.raises(PermissionError) as default:
        upd.fetch_latest("stable", "o/r", "tok", None)
    assert "only sent to" not in str(default.value) and "private or does not exist" in str(default.value)


# ── low: a rate limit is not "private or does not exist" ─────────────────────


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"}, json={}),
        httpx.Response(403, json={"message": "API rate limit exceeded for 1.2.3.4"}),
        httpx.Response(429, json={}),
    ],
)
def test_a_rate_limit_is_reported_as_one(monkeypatch: pytest.MonkeyPatch, resp: httpx.Response) -> None:
    resp.request = httpx.Request("GET", "https://x")
    _fake_get(monkeypatch, resp)
    with pytest.raises(PermissionError, match="rate limit"):
        upd.fetch_latest("stable", "o/r", None, None)


def test_a_plain_403_is_still_a_permission_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_get(
        monkeypatch,
        httpx.Response(403, headers={"x-ratelimit-remaining": "42"}, json={"message": "Resource not accessible"}),
    )
    with pytest.raises(PermissionError, match="private or does not exist"):
        upd.fetch_latest("stable", "o/r", None, None)


# ── low: uv's stderr is not swallowed ────────────────────────────────────────


def test_a_failed_uv_step_names_the_reason(data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rel, _ = _release(monkeypatch, [WHEEL, REQS])
    uv = _fake_uv(
        tmp_path, 'if [ "$2" = install ]; then echo "error: no matching distribution found for foo" >&2; exit 2; fi'
    )
    with pytest.raises(subprocess.CalledProcessError) as err:  # the type the CLI already catches
        upd.install_release(rel, None, uv=uv)
    assert "no matching distribution found for foo" in str(err.value) and "exited 2" in str(err.value)
    assert not (data / "versions" / "9.0.0").exists()


def test_cli_update_failure_shows_the_uv_error(data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rel, _ = _release(monkeypatch, [WHEEL, REQS])
    uv = _fake_uv(tmp_path, 'if [ "$2" = install ]; then echo "error: hash mismatch" >&2; exit 1; fi')
    monkeypatch.setattr(upd, "uv_path", lambda: uv)
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: rel)
    monkeypatch.setattr(upd, "github_token", lambda: None)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 1 and "update failed" in r.output and "hash mismatch" in r.output, r.output


# ── low: `update --check` gives a verdict ────────────────────────────────────


@pytest.mark.parametrize(("latest", "verdict"), [("9.9.9", "Update available"), ("1.0.0", "Already up to date")])
def test_update_check_prints_a_verdict_and_exits_zero(
    data: Path, monkeypatch: pytest.MonkeyPatch, latest: str, verdict: str
) -> None:
    rel = upd.Release(tag=f"v{latest}", version=latest, body="notes", prerelease=False, assets={})
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: rel)
    monkeypatch.setattr(upd, "github_token", lambda: None)
    monkeypatch.setattr(upd, "current_version", lambda: "1.0.0")
    monkeypatch.setattr(upd, "install_release", lambda *a, **k: pytest.fail("--check must not install"))
    r = CliRunner().invoke(cli, ["update", "--check"])
    assert r.exit_code == 0 and verdict in r.output, r.output


# ── 12: `service install` fails when systemd refuses, and restarts a running unit ─


def _fake_systemctl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: tuple[str, ...] = ()):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    calls: list[tuple[str, ...]] = []

    def fake(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        bad = args[0] in fail
        return subprocess.CompletedProcess(args, 1 if bad else 0, "", "Failed to connect to bus" if bad else "")

    monkeypatch.setattr(service, "_systemctl", fake)
    return calls


def test_install_restarts_a_running_unit_after_the_reload_and_before_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_systemctl(monkeypatch, tmp_path)
    out = service.install()
    assert calls == [("daemon-reload",), ("try-restart", service.UNIT_NAME), ("enable", "--now", service.UNIT_NAME)]
    assert any("try-restart" in line for line in out) and not any("failed" in line for line in out)
    assert any("try-restart" in line for line in service.install(dry_run=True))


def test_install_raises_when_a_systemctl_step_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_systemctl(monkeypatch, tmp_path, fail=("enable",))
    with pytest.raises(service.ServiceError, match="enable --now k3code.service failed: Failed to connect to bus"):
        service.install()
    assert service.unit_path().is_file()  # the files stay written; only the exit status changes


def test_cli_service_install_exits_nonzero_when_nothing_was_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_systemctl(monkeypatch, tmp_path, fail=("daemon-reload",))
    r = CliRunner().invoke(cli, ["service", "install"])
    assert r.exit_code != 0 and "daemon-reload failed" in r.output, r.output


# ── low: the goal watchdog's lookup uses an index ───────────────────────────


def test_goal_status_lookup_does_not_scan_the_transcripts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    for sid, status in (("a", "active"), ("b", "done"), ("c", "paused")):
        sess = store.create(title=sid)
        sess.meta["goal"] = {"goal": "g", "status": status}
        store.save(sess)
    plan = store._db.execute(
        "EXPLAIN QUERY PLAN SELECT session_id FROM sessions WHERE json_extract(meta, '$.goal.status') IN (?, ?)",
        ("active", "paused"),
    ).fetchall()
    assert any("sessions_goal_status" in str(row) for row in plan), plan
    assert sorted(s.meta["goal"]["status"] for s in store.with_goal_status("active", "paused")) == ["active", "paused"]
    SessionStore(tmp_path / "sessions.db")  # an existing database opens again with the index in place
