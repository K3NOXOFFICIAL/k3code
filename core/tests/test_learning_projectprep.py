import json

import pytest

from k3code import paths
from k3code.autonomy.proposals import ProposalStore
from k3code.learning import projectprep, projectstate
from learn_helpers import FakeCaller, FakeClock


def make(tmp_path, files: dict[str, str]):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


MATRIX = [
    (
        "python-uv",
        {"pyproject.toml": "[project]\nname='a'\n[tool.ruff]\n[build-system]\nrequires=[]\n", "uv.lock": ""},
        dict(language="python", package_manager="uv", test="uv run pytest", lint="uv run ruff check ."),
    ),
    (
        "node-npm",
        {
            "package.json": json.dumps({"scripts": {"test": "jest", "lint": "eslint .", "build": "tsc"}}),
            "package-lock.json": "{}",
        },
        dict(language="javascript", package_manager="npm", test="npm test", lint="npm run lint", build="npm run build"),
    ),
    ("go", {"go.mod": "module x\n"}, dict(language="go", test="go test ./...", build="go build ./...")),
    (
        "rust",
        {"Cargo.toml": "[workspace]\nmembers=[]\n"},
        dict(language="rust", package_manager="cargo", test="cargo test", monorepo=True),
    ),
]


@pytest.mark.parametrize(("name", "files", "expect"), MATRIX, ids=[m[0] for m in MATRIX])
def test_detection_matrix(tmp_path, name, files, expect):
    info = projectprep.detect(make(tmp_path, files))
    for k, v in expect.items():
        assert info[k] == v, (k, info)


def test_detects_ci_docker_monorepo(tmp_path):
    make(
        tmp_path,
        {"package.json": '{"workspaces": ["a"]}', ".github/workflows/ci.yml": "on: push", "Dockerfile": "FROM x"},
    )
    info = projectprep.detect(tmp_path)
    assert info["ci"] == "github-actions" and info["docker"] and info["monorepo"]


async def test_prepare_writes_only_k3code_state_until_accepted(tmp_path):
    # WS7: project.json and accepted rules live under $K3CODE_HOME/projects/<project>/, never in the repository
    repo = make(tmp_path / "repo", {"package.json": json.dumps({"scripts": {"test": "jest"}}), "src/a.js": "x"})
    store = ProposalStore(tmp_path / "home")
    clock = FakeClock()
    props = await projectprep.prepare(
        repo, store=store, caller=FakeCaller("# Notes\nRun npm test please\n"), clock=clock
    )
    assert sorted(str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file()) == ["package.json", "src/a.js"]
    state = projectprep.project_json_path(repo)
    assert state.is_relative_to(paths.home()) and not state.is_relative_to(repo)
    meta = json.loads(state.read_text())
    assert meta["info"]["language"] == "javascript" and meta["detected_at"] == clock()
    assert not projectprep.needs_prep(repo)
    texts = [p.text for p in props]
    assert any("K3CODE.md" in t for t in texts) and any("allow `npm test` without asking" in t for t in texts)
    assert any("every night" in t for t in texts) and any("risks" in t for t in texts)  # no CI -> risk
    # accept: draft the memory (cheap tier; the one repository write) and add the allow rule (k3code state)
    by = {p.payload.get("type") or p.payload["op"]: p for p in props}
    msg = await projectprep.apply(by["draft_memory"].payload, FakeCaller("# Notes\nRun npm test please\n"))
    assert "wrote" in msg and "npm test" in (repo / "K3CODE.md").read_text()
    await projectprep.apply(by["rule"].payload)
    assert "npm test" in projectstate.rules_path(repo).read_text() and not (repo / ".k3code").exists()


async def test_secret_risk_flagged_without_leaking_value(tmp_path):
    make(
        tmp_path,
        {"go.mod": "module x", "main_test.go": "package x", "config.txt": "key=AKIAABCDEFGHIJKLMNOP", ".env": "A=1"},
    )
    store = ProposalStore(tmp_path / "home")
    props = await projectprep.prepare(tmp_path, store=store)
    risk = [p for p in props if p.payload["op"] == "ack"][0].text
    assert "aws-key in config.txt" in risk and ".env" in risk and "AKIAABCDEFGH" not in risk


async def test_no_makefile_write_into_the_repo_and_scratch_dirs_are_skipped(tmp_path):
    # WS7: the Makefile proposal wrote into the repository; the only repository write left is K3CODE.md
    make(tmp_path, {"go.mod": "module x"})
    store = ProposalStore(tmp_path / "home")
    props = await projectprep.prepare(tmp_path, store=store, preferences=["always adds a Makefile"])
    assert not any("Makefile" in p.text for p in props)
    empty = tmp_path / "scratch"
    empty.mkdir()
    assert await projectprep.prepare(empty, store=store) == []
    assert not (empty / ".k3code").exists() and not projectstate.state_path(empty).exists()
