"""The assembled system prompt can be dumped, hashed and pinned (K3NOX-250, GitHub #166/2)."""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from k3code import debugdump, prompting
from k3code.cli import cli
from k3code.config import Settings
from k3code.gateway.server import LiveSession
from test_gateway import make_server

SNAPSHOT = Path(__file__).parent / "snapshots" / "prompt_hashes.json"
UPDATE = "K3CODE_UPDATE_SNAPSHOTS"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    # the prompt's "Session scope" names the working directory: under $HOME it reads `~/proj`, the same every run
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(proj)
    monkeypatch.delenv("K3CODE_PROJECT_DIR", raising=False)
    return proj


def _dump(*args: str) -> str:
    r = CliRunner().invoke(cli, ["prompt", "dump", *args])
    assert r.exit_code == 0, r.output
    return r.output


def test_a_change_to_the_prompt_needs_a_snapshot_update(project: Path) -> None:
    # Pins the base prompt and every mode's assembled prompt (fresh home, empty project, default config).
    got = {
        mode: {"hash": v["hash"], "chars": v["chars"]}
        for mode, v in prompting.prompt_fingerprints(Settings(), project).items()
    }
    if os.environ.get(UPDATE):
        SNAPSHOT.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    changed = sorted(m for m in {*got, *want} if got.get(m) != want.get(m))
    assert not changed, (
        f"the system prompt changed for: {', '.join(changed)}. If that is intended, run "
        f"`{UPDATE}=1 uv run pytest tests/test_prompt_dump.py` and commit tests/snapshots/prompt_hashes.json."
    )


def test_every_mode_is_dumped_and_hashed(project: Path) -> None:
    modes = prompting.mode_names(project)
    assert {"interactive", "headless", "plan"} <= set(modes)
    assert any(m.startswith("subagent:") for m in modes)
    for mode in modes:
        text = _dump("--mode", mode)
        assert text.startswith(("# k3code System Prompt", "[agent:")), mode
        assert _dump("--mode", mode, "--hash").strip() == prompting.prompt_hash(text)


def test_dump_matches_what_the_loops_are_given(project: Path) -> None:
    config = Settings()
    built = prompting.build_system_prompt(prompting.base_prompt(), cwd=project, config=config)
    assert _dump() == built == _dump("--mode", "headless")
    from k3code.autonomy.plan_first import PLAN_ADDENDUM

    plan = prompting.build_system_prompt(
        prompting.base_prompt(), cwd=project, config=config, skills=False, advisor=False
    )
    assert _dump("--mode", "plan") == plan + PLAN_ADDENDUM


def test_sections_lists_each_part_with_its_size_and_the_tools(project: Path) -> None:
    out = _dump("--sections", "--tools")
    for name in ("base", "tool schemas", "total"):
        assert any(line.startswith(name) for line in out.splitlines()), out
    assert f"sha256 {prompting.prompt_hash(_dump())}" in out
    assert '"name": "read"' in _dump("--tools")


def test_an_unknown_mode_is_a_usage_error_naming_the_modes(project: Path) -> None:
    r = CliRunner().invoke(cli, ["prompt", "dump", "--mode", "nope"])
    assert r.exit_code == 2
    assert "unknown mode 'nope'" in r.output and "subagent:" in r.output


def test_the_headless_and_gateway_loaders_read_the_same_base_prompt() -> None:
    import k3code.cli as cli_mod
    from k3code.gateway import server as gateway

    server = make_server()
    stored = server.store.create(model="default", cwd=str(Path.cwd()))
    assert LiveSession(stored.session_id, stored, server).system_prompt == prompting.base_prompt()
    assert not hasattr(cli_mod, "_load_system_prompt")  # one loader: prompting.base_prompt
    assert not hasattr(gateway, "_load_system_prompt")


async def test_the_debug_bundle_carries_the_prompt_hashes_but_not_the_text(project: Path, tmp_path: Path) -> None:
    path = await debugdump.write_bundle(make_server(), 10, home=tmp_path / "home", probe=False)
    with tarfile.open(path) as tar:
        fp = json.loads(tar.extractfile("prompt.json").read())
    text = prompting.build_system_prompt(prompting.base_prompt(), cwd=project, config=Settings())
    assert fp["interactive"]["hash"] == prompting.prompt_hash(text)[:16]
    assert fp["base"]["hash"] == prompting.prompt_hash(prompting.base_prompt())[:16]
    assert "base" in fp["interactive"]["sections"]
    assert prompting.base_prompt().splitlines()[0] not in json.dumps(fp)
