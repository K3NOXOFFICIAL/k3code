"""Updates install the tested dependency set (hashed requirements from the release) and keep Playwright."""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code import update as upd
from k3code.cli import cli

WHEEL = "k3code-9.0.0-py3-none-any.whl"
REQS = "k3code-9.0.0-requirements.txt"


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    return tmp_path / "data"


@pytest.fixture
def fake_uv(tmp_path: Path) -> tuple[str, Path]:
    """A ``uv`` that records its arguments and creates the venv directory it is asked for."""
    log = tmp_path / "uv.log"
    exe = tmp_path / "bin" / "uv"
    exe.parent.mkdir()
    exe.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nif [ "$1" = venv ]; then mkdir -p "$4/bin"; fi\n')
    exe.chmod(0o755)
    return str(exe), log


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _release(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> upd.Release:
    content = {WHEEL: b"wheel", REQS: b"httpx==0.28.1 --hash=sha256:" + b"0" * 64 + b"\n"}
    sums = "".join(f"{hashlib.sha256(content[n]).hexdigest()}  {n}\n" for n in names)
    by_url = {**{f"u/{n}": content[n] for n in names}, "u/SHA256SUMS": sums.encode()}
    monkeypatch.setattr(upd, "_download", lambda url, dest, tok: dest.write_bytes(by_url[url]))
    assets = {n: f"u/{n}" for n in [*names, "SHA256SUMS"]}
    return upd.Release(tag="v9.0.0", version="9.0.0", body="", prerelease=False, assets=assets)


def test_release_installs_the_locked_requirements_then_the_wheel_without_deps(
    data: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: tuple[str, Path]
) -> None:
    uv, log = fake_uv
    vdir = upd.install_release(_release(monkeypatch, [WHEEL, REQS]), None, uv=uv)
    py = vdir / "venv" / "bin" / "python"
    calls = _calls(log)
    assert calls[0].startswith("venv ")
    assert calls[1] == f"pip install --python {py} --require-hashes -r {vdir / '.dl' / REQS}"
    assert calls[2] == f"pip install --python {py} --no-deps {vdir / '.dl' / WHEEL}"
    assert len(calls) == 3 and (vdir / ".complete").is_file()


def test_release_without_requirements_is_refused_unless_allowed(
    data: Path, monkeypatch: pytest.MonkeyPatch, fake_uv: tuple[str, Path]
) -> None:
    uv, log = fake_uv
    rel = _release(monkeypatch, [WHEEL])
    downloaded: list[str] = []
    real = upd._download
    monkeypatch.setattr(upd, "_download", lambda url, dest, tok: downloaded.append(url) or real(url, dest, tok))
    with pytest.raises(upd.UnpinnedReleaseError, match="--allow-unpinned"):
        upd.install_release(rel, None, uv=uv)
    assert downloaded == [] and _calls(log) == [] and not (data / "versions" / "9.0.0").exists()
    vdir = upd.install_release(rel, None, uv=uv, allow_unpinned=True)  # an older release, as before
    assert _calls(log)[1:] == [f"pip install --python {vdir / 'venv' / 'bin' / 'python'} {vdir / '.dl' / WHEEL}"]


def test_cli_refuses_an_unpinned_release_and_passes_the_override(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rel = _release(monkeypatch, [WHEEL])
    monkeypatch.setattr(upd, "fetch_latest", lambda *a, **k: rel)
    r = CliRunner().invoke(cli, ["update", "--yes"])
    assert r.exit_code == 1 and "update refused" in r.output and "--allow-unpinned" in r.output, r.output
    assert "Traceback" not in r.output and not (data / "versions" / "9.0.0").exists()

    seen: list[bool] = []
    monkeypatch.setattr(upd, "install_release", lambda rel, tok, **kw: seen.append(kw["allow_unpinned"]))
    monkeypatch.setattr(upd, "activate", lambda ver: upd.UpdateResult(True, ver, f"updated to {ver}"))
    monkeypatch.setattr(upd, "prune", lambda: [])
    r = CliRunner().invoke(cli, ["update", "--yes", "--allow-unpinned"])
    assert r.exit_code == 0 and seen == [True], r.output


def _venv(data: Path, ver: str, playwright: str | None) -> Path:
    """A version dir whose ``python`` answers the Playwright probe: the version if installed, else an import error."""
    d = data / "versions" / ver
    bindir = d / "venv" / "bin"
    bindir.mkdir(parents=True)
    k3 = bindir / "k3code"
    k3.write_text(
        f"#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'k3code, version {ver}'; exit 0; fi\n"
        'echo \'{"summary": {"fail": 0}, "checks": []}\'\n'
    )
    answer = f"echo {playwright}; exit 0" if playwright else "echo 'No module named playwright' >&2; exit 1"
    py = bindir / "python"
    py.write_text(f'#!/bin/sh\ncase "$2" in *playwright*) {answer} ;; esac\nexit 2\n')
    k3.chmod(0o755)
    py.chmod(0o755)
    return d


def test_carry_playwright_installs_the_same_version_into_the_new_venv(data: Path, fake_uv: tuple[str, Path]) -> None:
    uv, log = fake_uv
    old, new = _venv(data, "1.0.0", "1.63.0"), _venv(data, "1.1.0", None)
    msg = upd.carry_playwright(old, new, uv=uv)
    assert msg is not None and "1.63.0" in msg
    assert _calls(log) == [f"pip install --quiet --python {new / 'venv' / 'bin' / 'python'} playwright==1.63.0"]


def test_carry_playwright_does_nothing_when_the_old_venv_has_none_or_the_new_has_one(
    data: Path, fake_uv: tuple[str, Path]
) -> None:
    uv, log = fake_uv
    assert upd.carry_playwright(_venv(data, "1.0.0", None), _venv(data, "1.1.0", None), uv=uv) is None
    assert upd.carry_playwright(_venv(data, "2.0.0", "1.63.0"), _venv(data, "2.1.0", "1.64.0"), uv=uv) is None
    assert upd.carry_playwright(data / "versions" / "nope", _venv(data, "3.0.0", None), uv=uv) is None
    assert _calls(log) == []


def test_activate_carries_playwright_before_the_smoke_test(data: Path, fake_uv: tuple[str, Path]) -> None:
    uv, log = fake_uv
    _venv(data, "1.0.0", "1.63.0")
    upd.switch_to("1.0.0")
    _venv(data, "1.1.0", None)
    order: list[str] = []

    def smoke(vdir: Path) -> tuple[bool, str]:
        order.append("smoke:" + ",".join(_calls(log)))
        return upd.smoke_test(vdir)

    res = upd.activate(
        "1.1.0", daemon_installed=lambda: False, smoke=smoke, carry=functools.partial(upd.carry_playwright, uv=uv)
    )
    assert res.ok and upd.current_version() == "1.1.0", res.message
    assert order and "playwright==1.63.0" in order[0]  # installed before the smoke test ran
    assert any("carried Playwright 1.63.0" in line for line in res.log)


def test_a_failed_carry_is_reported_but_does_not_block_the_update(data: Path) -> None:
    _venv(data, "1.0.0", "1.63.0")
    upd.switch_to("1.0.0")
    _venv(data, "1.1.0", None)
    res = upd.activate(
        "1.1.0", daemon_installed=lambda: False, carry=lambda old, new: "warning: could not install Playwright"
    )
    assert res.ok and upd.current_version() == "1.1.0"
    assert "warning: could not install Playwright" in res.message
