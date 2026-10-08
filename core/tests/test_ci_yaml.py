from __future__ import annotations

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text())


def test_ci_jobs() -> None:
    wf = load("ci.yml")
    assert set(wf["jobs"]) == {"core", "tui", "panes", "vendor_check"}
    for job in wf["jobs"].values():
        assert job["runs-on"] and job["steps"]


def test_installer_jobs_run_only_when_the_installer_changes() -> None:
    # macOS and Windows minutes cost several times a Linux minute, so they sit behind a paths filter.
    wf = load("installer.yml")
    assert set(wf["jobs"]) == {"installer-macos", "installer-windows"}
    for job in wf["jobs"].values():
        assert job["runs-on"] and job["steps"]
    for event in ("push", "pull_request"):
        assert "install/**" in wf[True][event]["paths"]


def test_release_triggers_on_tags_and_ships_assets() -> None:
    wf = load("release.yml")
    assert wf[True]["push"]["tags"] == ["v*"]  # PyYAML parses the key `on` as True
    text = (WF / "release.yml").read_text()
    for needle in ("SHA256SUMS", "k3-$os-$arch", "k3code-tui-", "uv build --wheel", "gh release create"):
        assert needle in text
