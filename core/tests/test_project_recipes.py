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


def _rust_hook_command(stack: dict, root: Path) -> str:
    (hook,) = [s for s in recipes.suggest(stack, root) if s.kind == "hook"]
    return hook.payload["entry"]["command"]


def test_hostile_cargo_edition_never_reaches_the_hook_command(tmp_path):
    hostile = "2021; echo injected"
    repo = make(tmp_path / "r", {"Cargo.toml": f"[package]\nname='x'\nedition='{hostile}'\n"})
    rust = by_id(stacks.scan(repo))["rust"]
    assert rust["extra"]["edition"] == ""  # not a year: dropped at detection
    cmd = _rust_hook_command(rust, repo)
    assert "injected" not in cmd and "--edition" not in cmd
    # a value stored by an older scan is still quoted into a single argument
    stored = {**rust, "extra": {"edition": hostile}}
    assert "rustfmt --edition '2021; echo injected' \"$f\"" in _rust_hook_command(stored, repo)
    plain = {**rust, "extra": {"edition": "2021"}}
    assert 'rustfmt --edition 2021 "$f"' in _rust_hook_command(plain, repo)


async def test_formatter_hook_command_formats_only_matching_files_under_its_dir(tmp_path):
    api = make(tmp_path / "repo", {"api/a.py": "", "api/b.md": "", "c.py": ""}) / "api"
    seen = tmp_path / "seen.txt"
    cmd = recipes.formatter_hook_command(api, f"sh -c 'echo \"$0\" >> {seen}'", (".py",))
    runner = userhooks.HookRunner([userhooks.Hook("PostToolUse", cmd, "edit|write")], cwd=tmp_path / "repo")
    for path in ("api/a.py", "api/b.md", "c.py"):
        out = await runner.run("PostToolUse", {"tool_input": {"path": path}}, tool_name="edit")
        assert not out.blocked
    assert seen.read_text().splitlines() == [str((api / "a.py").resolve())]


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


# ── project facts, skills ranking, onboarding, /project ──


async def test_facts_section_is_injected_without_memory_and_is_stable(tmp_path):
    from k3code.config import Settings
    from k3code.prompting import build_system_prompt

    repo = make(tmp_path / "repo", {**MONOREPO, ".git/HEAD": ""})  # a session in api/ shares the repo's state
    assert projectprep.facts_prompt(repo) == ""
    await projectprep.prepare(repo, store=ProposalStore(tmp_path / "home"))
    facts = projectprep.facts_prompt(repo)
    assert facts.startswith("## Project facts") and len(facts.splitlines()) <= projectprep.FACTS_MAX_LINES
    assert "python (uv) in api/" in facts and "node (pnpm) in packages/web/" in facts
    assert "api/ python: test `uv run pytest`" in facts and "format `uv run ruff format .`" in facts
    assert not any((repo / n).exists() for n in ("K3CODE.md", "AGENTS.md", "CLAUDE.md"))  # no memory file
    one = build_system_prompt("BASE", cwd=repo / "api", config=Settings())
    two = build_system_prompt("BASE", cwd=repo / "api", config=Settings())
    assert facts in one and one == two


def test_facts_section_is_capped(tmp_path):
    many = {f"svc{i:02d}/go.mod": "module x\n" for i in range(40)}
    repo = make(tmp_path / "repo", {".git/HEAD": "", **many})
    projectstate.save(repo, {"stacks": stacks.scan(repo).stacks})
    lines = projectprep.facts_prompt(repo).splitlines()
    assert len(lines) == projectprep.FACTS_MAX_LINES and lines[-2].startswith("…and ")


def _skill(root: Path, name: str, description: str) -> None:
    make(root, {f"{name}/SKILL.md": f"---\nname: {name}\ndescription: {description}\n---\nbody\n"})


async def test_skills_index_prefers_stack_matches_then_recent_successful_use(tmp_path):
    from k3code.skills import skills_prompt

    sroot = tmp_path / "skills"
    for i in range(65):
        _skill(sroot, f"a-skill-{i:02d}", "generic helper")
    _skill(sroot, "zz-pytest-runner", "run the test suite")
    _skill(sroot, "zz-used-often", "generic helper")
    repo = make(tmp_path / "repo", {"pyproject.toml": "[project]\nname='a'\n"})
    before = skills_prompt(repo, [str(sroot)], limit=10)
    assert "zz-pytest-runner" not in before  # by name only, it is past the first 10
    await projectprep.prepare(repo, store=ProposalStore(tmp_path / "home"))
    ranked = skills_prompt(repo, [str(sroot)], limit=10)
    assert "zz-pytest-runner" in ranked and "zz-used-often" not in ranked
    from k3code.learning.curator import record_use

    record_use("zz-used-often")
    used = skills_prompt(repo, [str(sroot)], limit=10)
    assert "zz-used-often" in used and "zz-pytest-runner" in used
    assert used == skills_prompt(repo, [str(sroot)], limit=10)  # same input, same section


def test_onboarding_non_interactive_default_creates_no_proposals(tmp_path):
    from k3code.setup.prompter import AnswerPrompter
    from k3code.setup.steps import offer_project_recipes

    repo = make(tmp_path / "repo", MONOREPO)
    before = snapshot(repo)
    out = offer_project_recipes(AnswerPrompter({}), repo)
    assert out == {"recipes": "none", "accepted": []}
    assert ProposalStore(paths.home()).all() == [] and not projectstate.state_path(repo).exists()
    assert snapshot(repo) == before


def test_onboarding_accept_all_applies_the_recipes(tmp_path):
    from k3code.setup.prompter import AnswerPrompter
    from k3code.setup.steps import offer_project_recipes

    repo = make(tmp_path / "repo", MONOREPO)
    before = snapshot(repo)
    said: list[str] = []
    p = AnswerPrompter({"project_recipes": "accept_all"})
    p.say = said.append  # type: ignore[method-assign]
    out = offer_project_recipes(p, repo)
    assert out["accepted"] and any("python (uv) in api/" in s for s in said)
    store = ProposalStore(paths.home())
    assert {x.status for x in store.all() if x.payload.get("op") == "recipe"} == {"accepted"}
    assert "uv run pytest" in projectstate.rules_path(repo).read_text()
    assert snapshot(repo) == before


async def test_project_command_shows_stacks_and_pending_and_rescans(tmp_path):
    from types import SimpleNamespace

    from k3code.commands.project_cmd import ProjectCommand
    from k3code.config import Settings

    repo = make(tmp_path / "repo", {"go.mod": "module x\n", ".git/HEAD": ""})
    live = SimpleNamespace(stored=SimpleNamespace(cwd=str(repo)))
    store = ProposalStore(tmp_path / "home")
    ctx = SimpleNamespace(
        config=Settings(), autonomy=SimpleNamespace(proposals=store), sessions={"s1": live}, learning=None
    )
    cmd = ProjectCommand()
    empty = await cmd.handle(ctx, "s1", "")
    assert "No stacks detected" in empty["message"]
    first = await cmd.handle(ctx, "s1", "rescan")
    assert "new proposal" in first["message"] and "go: test `go test ./...`" in first["message"]
    assert first["pending"] and first["stacks"] == [{"id": "go", "dir": ".", "label": "go (go)"}]
    assert "nothing changed" in (await cmd.handle(ctx, "s1", "rescan"))["message"]
    make(repo, {"go.mod": "module x\n\ngo 1.22\n"})
    assert (await cmd.handle(ctx, "s1", ""))["changed"] is True
    assert "Usage" in (await cmd.handle(ctx, "s1", "bogus"))["message"]
