"""Project memory: K3CODE.md, AGENTS.md and CLAUDE.md along the directory chain, rules, @imports and the caps."""

from __future__ import annotations

from pathlib import Path

from k3code import memory
from k3code.autonomy.proposals import ProposalStore
from k3code.learning import projectprep
from k3code.memory import load_memory, memory_prompt


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def test_claude_md_is_read(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "CLAUDE.md", "CLAUDE-ONLY")
    assert [m.text for m in load_memory(root)] == ["CLAUDE-ONLY"]
    prompt = memory_prompt(root)
    assert "## Project memory (CLAUDE.md)" in prompt and "project instructions (from the repository):" in prompt


def test_symlinked_memory_files_cannot_reach_outside_the_repo_or_a_credential(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "alice"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("K3CODE_HOME", str(home / ".k3code"))
    key = _write(home / ".ssh" / "id_ed25519", "PRIVATE-KEY-MATERIAL")
    outside = _write(tmp_path / "outside.md", "OUTSIDE-FILE")
    root = _repo(tmp_path)
    (root / ".k3code" / "rules").mkdir(parents=True)
    (root / ".k3code" / "rules" / "a.md").symlink_to(key)
    (root / "pkg").mkdir()
    (root / "pkg" / "CLAUDE.md").symlink_to(outside)
    _write(root / "K3CODE.md", "REAL")
    blob = " ".join(m.text for m in load_memory(root / "pkg"))
    assert "PRIVATE-KEY-MATERIAL" not in blob and "OUTSIDE-FILE" not in blob and "REAL" in blob


def test_all_three_files_in_order_and_identical_ones_once(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "CLAUDE.md", "SHARED")
    _write(root / "AGENTS.md", "SHARED")  # the common AGENTS.md == CLAUDE.md copy
    _write(root / "K3CODE.md", "K3")
    assert [(m.path.name, m.text) for m in load_memory(root)] == [("K3CODE.md", "K3"), ("AGENTS.md", "SHARED")]


def test_hierarchy_root_to_cwd_nearest_last_with_rules_and_user(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    _write(tmp_path / "k3home" / "memory" / "USER.md", "USER")
    root = _repo(tmp_path)
    _write(root / "AGENTS.md", "ROOT")
    _write(root / "pkg" / "CLAUDE.md", "PKG")
    _write(root / "pkg" / "sub" / "K3CODE.md", "SUB")
    _write(root / "pkg" / "sibling" / "K3CODE.md", "NOT-ON-THE-PATH")
    _write(tmp_path / "AGENTS.md", "ABOVE-THE-REPO")
    _write(root / ".k3code" / "rules" / "b.md", "RULE-B")
    _write(root / ".k3code" / "rules" / "a.md", "RULE-A")
    cwd = root / "pkg" / "sub"
    files = load_memory(cwd)
    assert [(m.scope, m.text) for m in files] == [
        ("user", "USER"),
        ("rules", "RULE-A"),
        ("rules", "RULE-B"),
        ("project", "ROOT"),
        ("project", "PKG"),
        ("project", "SUB"),
    ]
    prompt = memory_prompt(cwd)
    assert prompt.index("ROOT") < prompt.index("PKG") < prompt.index("SUB")
    assert "## Project memory (pkg/CLAUDE.md)" in prompt and "## Project rules (.k3code/rules/a.md)" in prompt
    assert "NOT-ON-THE-PATH" not in prompt and "ABOVE-THE-REPO" not in prompt


def test_per_file_and_total_caps(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "K3CODE.md", "a" * 30_000)
    _write(root / "AGENTS.md", "b" * 15_000)
    _write(root / "CLAUDE.md", "c" * 15_000)
    prompt = memory_prompt(root)
    assert "a" * 20_000 in prompt and "a" * 20_001 not in prompt  # per-file cap
    assert "b" * 15_000 in prompt
    assert "c" * 4_900 in prompt and "c" * 5_001 not in prompt  # the rest of the 40k total (markers count too)
    assert prompt.count("…(truncated)") == 2


def test_total_cap_keeps_the_nearest_files_and_trims_the_root(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    for name, fill in (("K3CODE.md", "k"), ("AGENTS.md", "a"), ("CLAUDE.md", "c")):
        _write(root / name, fill * 20_000)
    _write(root / "pkg" / "K3CODE.md", "MOST SPECIFIC RULE: never touch pkg/gen")
    prompt = memory_prompt(root / "pkg")
    assert "## Project memory (pkg/K3CODE.md)" in prompt and "MOST SPECIFIC RULE: never touch pkg/gen" in prompt
    assert prompt.index("k" * 20_000) < prompt.index("MOST SPECIFIC")  # still nearest last in the prompt
    assert "a" * 19_000 in prompt and "c" * 20_000 not in prompt  # the last root file is what was cut
    assert memory.TOTAL_MARKER.format(n=1, cap=memory.MAX_MEMORY_TOTAL) in prompt
    assert len(prompt) < memory.MAX_MEMORY_TOTAL + 1_000


def test_total_cap_trims_the_farthest_file_first(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "K3CODE.md", "r" * 20_000)
    _write(root / "pkg" / "K3CODE.md", "p" * 10_000)
    _write(root / "pkg" / "sub" / "K3CODE.md", "s" * 20_000)
    prompt = memory_prompt(root / "pkg" / "sub")
    assert "s" * 20_000 in prompt and "p" * 10_000 in prompt  # the two nearest files whole
    assert "r" * 9_000 in prompt and "r" * 20_000 not in prompt  # the root file is what loses its tail
    assert prompt.index("r" * 9_000) < prompt.index("p" * 9_000) < prompt.index("s" * 9_000)
    assert memory.TOTAL_MARKER.format(n=1, cap=memory.MAX_MEMORY_TOTAL) not in prompt  # trimmed, none left out


def test_total_cap_keeps_the_users_memory(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    _write(tmp_path / "home" / "memory" / "USER.md", "USER PREFERENCE")
    for name in ("K3CODE.md", "AGENTS.md", "CLAUDE.md"):
        _write(root / name, name[0] * 20_000)
    prompt = memory_prompt(root)
    assert "USER PREFERENCE" in prompt


def test_import_relative_to_the_importing_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "docs" / "style.md", "STYLE\n@more/extra.md")
    _write(root / "docs" / "more" / "extra.md", "EXTRA")
    _write(root / "CLAUDE.md", "intro\n@docs/style.md\n```\n@docs/style.md\n```\n@property\nend")
    text = load_memory(root)[0].text
    assert text.splitlines()[:3] == ["intro", "STYLE", "EXTRA"]
    assert "```\n@docs/style.md\n```" in text  # inside a code fence: not an import
    assert "@property" in text  # names no file: kept as written
    assert text.endswith("end")


def test_import_depth_is_capped_at_five(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    for i in range(1, 8):
        _write(root / f"l{i}.md", f"L{i}\n@l{i + 1}.md")
    _write(root / "K3CODE.md", "TOP\n@l1.md")
    text = load_memory(root)[0].text
    assert all(f"L{i}" in text for i in range(1, 6))
    assert "L6" not in text and "imports nest at most 5 deep" in text


def test_import_cycle_is_cut(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "a.md", "A\n@b.md")
    _write(root / "b.md", "B\n@a.md")
    _write(root / "AGENTS.md", "@a.md")
    text = load_memory(root)[0].text
    assert text.count("A\n") == 1 and text.count("B") == 1 and "it imports itself" in text


def test_import_outside_the_repository_or_a_secret_is_refused(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    _write(tmp_path / "outside.md", "OUTSIDE-SECRET")
    home = Path.home()
    _write(home / "notes.md", "HOME-NOTES")
    _write(root / ".env", "TOKEN=abc")
    _write(root / "K3CODE.md", "@../outside.md\n@~/notes.md\n@.env\n@" + str(tmp_path / "outside.md"))
    text = load_memory(root)[0].text
    assert "OUTSIDE-SECRET" not in text and "HOME-NOTES" not in text and "TOKEN=abc" not in text
    assert text.count("not imported: outside the allowed directories") == 4


def test_user_memory_may_import_from_the_home_directory(tmp_path: Path, monkeypatch) -> None:
    k3home = Path.home() / ".k3code"
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    _write(Path.home() / "prefs.md", "MY-PREFS")
    _write(k3home / "memory" / "USER.md", "@~/prefs.md\n@" + str(_write(tmp_path / "x.md", "NOT-HOME")))
    text = load_memory(tmp_path / "nowhere")[0].text
    assert "MY-PREFS" in text and "NOT-HOME" not in text


async def test_projectprep_does_not_propose_k3code_md_over_claude_md(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "CLAUDE.md", "# Notes\nrun make test\n")
    _write(root / "Makefile", "test:\n\ttrue\n")
    props = await projectprep.prepare(root, store=ProposalStore(tmp_path / "store"))
    assert not [p for p in props if p.payload.get("op") == "draft_memory"]
