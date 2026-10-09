"""WS7 project recipes: multi-stack detection, k3code-side state, fingerprint re-scans and recipe proposals."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from k3code import mcpjson, paths, userhooks
from k3code.autonomy.proposals import ProposalStore
from k3code.learning import permrules, projectprep, projectstate, recipes, stacks
from k3code.permissions.state import PermissionState


def make(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """Every file and directory under ``root`` with its size, mtime and content hash."""
    out: dict[str, tuple[int, int, str]] = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
        out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, digest)
    return out


MONOREPO = {
    "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n",
    "api/pyproject.toml": "[project]\nname='api'\n[tool.ruff]\nline-length=100\n[tool.pytest.ini_options]\n",
    "api/uv.lock": "",
    "api/tests/test_x.py": "def test_x(): pass\n",
    "packages/web/package.json": json.dumps(
        {"scripts": {"test": "vitest", "lint": "eslint .", "build": "vite build"}, "devDependencies": {"prettier": "3"}}
    ),
    "packages/web/pnpm-lock.yaml": "",
}


def by_id(scan: stacks.Scan) -> dict[str, dict]:
    return {s["id"]: s for s in scan.stacks}


def test_monorepo_python_uv_and_node_pnpm_are_both_detected_with_commands(tmp_path):
    repo = make(tmp_path / "repo", MONOREPO)
    found = by_id(stacks.scan(repo))
    py, node = found["python"], found["node"]
    assert (py["dir"], py["manager"]) == ("api", "uv")  # first-level subdirectory
    assert py["commands"] == {
        "test": "uv run pytest",
        "lint": "uv run ruff check .",
        "format": "uv run ruff format .",
    }
    assert (node["dir"], node["manager"]) == ("packages/web", "pnpm")  # via the pnpm workspace declaration
    assert node["commands"] == {"test": "pnpm test", "lint": "pnpm run lint", "build": "pnpm run build"}
    info = stacks.legacy_info(stacks.scan(repo), repo)
    assert info["monorepo"] and info["language"] == "python"


def test_home_assistant_nix_flake_and_docker_compose(tmp_path):
    ha = make(
        tmp_path / "ha",
        {
            "configuration.yaml": "homeassistant:\n  name: Home\n  internal_url: http://ha.example:8123\n",
            "custom_components/thing/__init__.py": "",
        },
    )
    s = by_id(stacks.scan(ha))["home-assistant"]
    assert s["extra"]["url"] == "http://ha.example:8123" and s["commands"]["lint"].startswith("hass --script")
    plain = make(tmp_path / "plain", {"configuration.yaml": "server:\n  port: 1\n"})  # not HA: no marker content
    assert "home-assistant" not in by_id(stacks.scan(plain))

    nix = by_id(stacks.scan(make(tmp_path / "nix", {"flake.nix": "{ outputs = _: {}; }"})))["nix"]
    assert nix["manager"] == "flake" and nix["commands"]["test"] == "nix flake check"

    dock = by_id(stacks.scan(make(tmp_path / "dock", {"compose.yaml": "services: {}\n"})))["docker"]
    assert dock["manager"] == "compose" and dock["commands"] == {"build": "docker compose build"}


def test_more_stacks_from_the_table(tmp_path):
    repo = make(
        tmp_path / "r",
        {
            "go.mod": "module x\n",
            "Cargo.toml": "[package]\nname='x'\nedition='2021'\n",
            "main.tf": "",
            "ansible.cfg": "",
            "Gemfile": "gem 'rspec'\n",
            "composer.json": json.dumps({"scripts": {"test": "phpunit"}}),
            "app/app.csproj": "<Project/>",
            "pom.xml": "<project/>",
            "justfile": "test *args:\n  pytest\nlint:\n  ruff\n",
            ".gitlab-ci.yml": "x: 1\n",
            "deno.json": "{}",
        },
    )
    found = by_id(stacks.scan(repo))
    assert {"go", "rust", "terraform", "ansible", "ruby", "php", "dotnet", "java", "make", "ci"} <= set(found)
    assert found["rust"]["extra"]["edition"] == "2021" and found["ruby"]["commands"]["test"] == "bundle exec rspec"
    assert found["make"]["commands"] == {"test": "just test", "lint": "just lint"}
    assert found["ci"]["manager"] == "gitlab-ci" and found["dotnet"]["dir"] == "app"


def test_makefile_targets_are_matched_exactly(tmp_path):
    repo = make(tmp_path / "r", {"Makefile": "test-unit:\n\ttrue\ntesting:\n\ttrue\nlint := x\nfmt build:\n\ttrue\n"})
    assert by_id(stacks.scan(repo))["make"]["commands"] == {"format": "make fmt", "build": "make build"}


async def test_prepare_and_accepting_every_recipe_never_write_into_the_repo(tmp_path):
    repo = make(tmp_path / "repo", MONOREPO)
    before = snapshot(repo)
    store = ProposalStore(tmp_path / "home")
    props = await projectprep.prepare(repo, store=store)
    assert snapshot(repo) == before
    kinds = {p.kind for p in props}
    assert {"mcp", "hook", "rule", "commands"} <= kinds
    for p in props:
        if p.payload.get("op") == "recipe":
            await projectprep.apply(p.payload)
    assert snapshot(repo) == before
    assert projectstate.state_path(repo).is_relative_to(paths.home())


async def test_rules_are_exact_commands_never_launcher_wildcards(tmp_path):
    repo = make(tmp_path / "repo", MONOREPO)
    props = await projectprep.prepare(repo, store=ProposalStore(tmp_path / "home"))
    patterns = [r["pattern"] for p in props if p.kind == "rule" for r in p.payload["rules"]]
    assert set(patterns) == {"uv run pytest", "uv run ruff check .", "pnpm test", "pnpm run lint"}
    for pat in patterns:
        assert "*" not in pat and not permrules.unsafe_pattern(pat)
    assert recipes.exact_rules({"commands": {"test": "uv run pytest; rm -rf /"}}, ("test",)) == []
    rule = next(p for p in props if p.kind == "rule" and p.payload["stack"] == "python")
    await projectprep.apply(rule.payload)
    perms = PermissionState(cwd=repo)
    perms.reload()
    assert perms.decide("bash", {"command": "uv run pytest"}).action == "allow"
    assert perms.decide("bash", {"command": "uv run python -c 'import os'"}).action != "allow"
    other = PermissionState(cwd=make(tmp_path / "other", {"x.txt": ""}))
    other.reload()
    assert other.decide("bash", {"command": "uv run pytest"}).action != "allow"  # this project only


async def test_hook_proposal_is_a_valid_project_scoped_hook_entry(tmp_path):
    repo = make(tmp_path / "repo", MONOREPO)
    props = await projectprep.prepare(repo, store=ProposalStore(tmp_path / "home"))
    hooks = {p.payload["stack"]: p for p in props if p.kind == "hook"}
    assert set(hooks) == {"python", "node"}  # ruff and prettier are configured
    entry = hooks["python"].payload["entry"]
    parsed = userhooks.parse({"PostToolUse": [entry]}, "user")
    assert len(parsed) == 1
    h = parsed[0]
    assert (h.event, h.matcher, h.project) == ("PostToolUse", "edit|write", str(repo.resolve()))
    assert "uv run ruff format" in h.command and h.matches("edit") and not h.matches("bash")
    await projectprep.apply(hooks["python"].payload)
    data = yaml.safe_load(paths.user_config_path().read_text())
    assert data["hooks"]["PostToolUse"] == [entry]
    assert [x.command for x in userhooks.load(repo / "api").hooks] == [entry["command"]]
    assert userhooks.load(tmp_path).hooks == []  # outside the project: not loaded


async def test_formatter_hook_command_formats_only_matching_files_under_its_dir(tmp_path):
    api = make(tmp_path / "repo", {"api/a.py": "", "api/b.md": "", "c.py": ""}) / "api"
    seen = tmp_path / "seen.txt"
    cmd = recipes.formatter_hook_command(api, f"sh -c 'echo \"$0\" >> {seen}'", (".py",))
    runner = userhooks.HookRunner([userhooks.Hook("PostToolUse", cmd, "edit|write")], cwd=tmp_path / "repo")
    for path in ("api/a.py", "api/b.md", "c.py"):
        out = await runner.run("PostToolUse", {"tool_input": {"path": path}}, tool_name="edit")
        assert not out.blocked
    assert seen.read_text().splitlines() == [str(api / "a.py")]


async def test_fingerprint_change_proposes_only_what_is_new(tmp_path):
    repo = make(tmp_path / "repo", {"pyproject.toml": "[project]\nname='a'\n", "uv.lock": ""})
    store = ProposalStore(tmp_path / "home")
    first = await projectprep.prepare(repo, store=store)
    assert first and not projectprep.needs_prep(repo)
    assert await projectprep.prepare(repo, store=store) == []  # nothing changed: no scan result, no proposals
    make(repo, {"svc/go.mod": "module svc\n"})
    assert projectprep.needs_prep(repo)
    again = await projectprep.prepare(repo, store=store)
    assert again and {p.payload.get("stack") for p in again} == {"go"}
    assert all(p.payload.get("op") == "recipe" for p in again)  # no K3CODE.md/nightly/risk cards again
    state = projectstate.load(repo)
    assert "go@svc" in state["proposed"] and "python@." in state["proposed"]


async def test_legacy_repo_local_project_json_is_read_not_written(tmp_path):
    repo = make(tmp_path / "repo", {"go.mod": "module x\n", ".k3code/project.json": json.dumps({"language": "go"})})
    before = snapshot(repo)
    props = await projectprep.prepare(repo, store=ProposalStore(tmp_path / "home"))
    assert snapshot(repo) == before
    assert props and all(p.payload.get("op") == "recipe" for p in props)  # prepared before: only the new recipes
    assert projectstate.load(repo)["migrated_from"].endswith(".k3code/project.json")


async def test_home_assistant_mcp_references_a_token_variable_and_is_scoped(tmp_path):
    ha = make(tmp_path / "ha", {"configuration.yaml": "default_config:\n", "custom_components/x/a.py": ""})
    props = await projectprep.prepare(ha, store=ProposalStore(tmp_path / "home"))
    mcp = next(p for p in props if p.kind == "mcp")
    assert mcp.payload["server"] == {"url": "http://homeassistant.local:8123/api/mcp", "bearer_env": "HASS_TOKEN"}
    assert "HASS_TOKEN" in mcp.text
    await projectprep.apply(mcp.payload)
    assert mcpjson.merged({}, ha)["home-assistant"].bearer_env == "HASS_TOKEN"
    assert "home-assistant" not in mcpjson.merged({}, tmp_path / "elsewhere")
