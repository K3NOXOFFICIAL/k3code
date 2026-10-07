import json

import pytest

from k3code.autonomy.proposals import ProposalStore
from k3code.learning import projectprep
from learn_helpers import FakeCaller, FakeClock


def make(tmp_path, files: dict[str, str]):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


MATRIX = [
    ("python-uv", {"pyproject.toml": "[project]\nname='a'\n[tool.ruff]\n[build-system]\nrequires=[]\n", "uv.lock": ""},
     dict(language="python", package_manager="uv", test="uv run pytest", lint="uv run ruff check .")),
    ("node-npm", {"package.json": json.dumps({"scripts": {"test": "jest", "lint": "eslint .", "build": "tsc"}}),
                  "package-lock.json": "{}"},
     dict(language="javascript", package_manager="npm", test="npm test", lint="npm run lint", build="npm run build")),
    ("go", {"go.mod": "module x\n"}, dict(language="go", test="go test ./...", build="go build ./...")),
    ("rust", {"Cargo.toml": "[workspace]\nmembers=[]\n"},
     dict(language="rust", package_manager="cargo", test="cargo test", monorepo=True)),
]


@pytest.mark.parametrize(("name", "files", "expect"), MATRIX, ids=[m[0] for m in MATRIX])
def test_detection_matrix(tmp_path, name, files, expect):
    info = projectprep.detect(make(tmp_path, files))
    for k, v in expect.items():
        assert info[k] == v, (k, info)


def test_detects_ci_docker_monorepo(tmp_path):
    make(tmp_path, {"package.json": '{"workspaces": ["a"]}', ".github/workflows/ci.yml": "on: push",
                    "Dockerfile": "FROM x"})
    info = projectprep.detect(tmp_path)
    assert info["ci"] == "github-actions" and info["docker"] and info["monorepo"]


async def test_prepare_writes_only_project_json_until_accepted(tmp_path):
    make(tmp_path, {"package.json": json.dumps({"scripts": {"test": "jest"}}), "src/a.js": "x"})
    store = ProposalStore(tmp_path / "home")
    clock = FakeClock()
    props = await projectprep.prepare(tmp_path, store=store, caller=FakeCaller("# Notes\nRun npm test please\n"),
                                      clock=clock)
    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
                     if p.is_file() and ".k3code" in p.parts)
    assert written == [".k3code/project.json"]
    assert not (tmp_path / "K3CODE.md").exists() and not (tmp_path / ".k3code" / "config.yaml").exists()
    meta = json.loads((tmp_path / ".k3code" / "project.json").read_text())
    assert meta["language"] == "javascript" and meta["detected_at"] == clock()
    assert not projectprep.needs_prep(tmp_path)
    texts = [p.text for p in props]
    assert any("K3CODE.md" in t for t in texts) and any("safe commands" in t for t in texts)
    assert any("every night" in t for t in texts) and any("risks" in t for t in texts)  # no CI -> risk
    assert all(p.kind == "project_setup" for p in props)
    # accept: draft the memory (cheap tier) and add the allow rules
    by = {p.payload["op"]: p for p in props}
    msg = await projectprep.apply(by["draft_memory"].payload, FakeCaller("# Notes\nRun npm test please\n"))
    assert "wrote" in msg and "npm test" in (tmp_path / "K3CODE.md").read_text()
    await projectprep.apply(by["allow_rules"].payload)
    assert "npm test *" in (tmp_path / ".k3code" / "config.yaml").read_text()


async def test_secret_risk_flagged_without_leaking_value(tmp_path):
    make(tmp_path, {"go.mod": "module x", "main_test.go": "package x", "config.txt": "key=AKIAABCDEFGHIJKLMNOP",
                    ".env": "A=1"})
    store = ProposalStore(tmp_path / "home")
    props = await projectprep.prepare(tmp_path, store=store)
    risk = [p for p in props if p.payload["op"] == "ack"][0].text
    assert "aws-key in config.txt" in risk and ".env" in risk and "AKIAABCDEFGH" not in risk


async def test_learned_preference_offers_makefile_and_skips_scratch_dirs(tmp_path):
    make(tmp_path, {"go.mod": "module x"})
    store = ProposalStore(tmp_path / "home")
    props = await projectprep.prepare(tmp_path, store=store, preferences=["always adds a Makefile"])
    assert any("Makefile" in p.text for p in props)
    empty = tmp_path / "scratch"
    empty.mkdir()
    assert await projectprep.prepare(empty, store=store) == []
    assert not (empty / ".k3code").exists()
