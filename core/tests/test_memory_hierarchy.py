"""Project memory: K3CODE.md, AGENTS.md and CLAUDE.md along the directory chain, rules, @imports and the caps."""

from __future__ import annotations

import tracemalloc
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


def _write_big(path: Path, megabytes: int) -> Path:
    """A text file of ``megabytes`` MB, written a megabyte at a time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    megabyte = ("x" * 1023 + "\n") * 1024
    with path.open("w", encoding="utf-8") as f:
        for _ in range(megabytes):
            f.write(megabyte)
    return path


def _peak_bytes(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_a_huge_memory_file_is_not_read_whole(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write_big(root / "K3CODE.md", 32)
    prompt = ""

    def build() -> None:
        nonlocal prompt
        prompt = memory_prompt(root)

    assert _peak_bytes(build) < 4 << 20  # reading it whole needs 32 MB as bytes plus 32 MB as text
    assert "x" * 1023 in prompt and prompt.count("x") <= 20_000 and "…(truncated)" in prompt


def test_a_huge_import_is_refused_not_read(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write_big(root / "docs" / "huge.log", 32)
    _write(root / "K3CODE.md", "before\n@docs/huge.log\nafter")
    text = ""

    def build() -> None:
        nonlocal text
        text = load_memory(root)[0].text

    assert _peak_bytes(build) < 4 << 20
    assert text.splitlines() == ["before", "(@docs/huge.log not imported: larger than 1 MB)", "after"]


def test_a_binary_import_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 8)
    _write(root / "K3CODE.md", "before\n@logo.png\nafter")
    assert load_memory(root)[0].text.splitlines() == ["before", "(@logo.png not imported: not a text file)", "after"]


def test_imports_stop_at_the_total_cap(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    for i in range(5):
        _write(root / f"part{i}.md", f"P{i}\n" + "y" * 600_000)  # under the 1 MB import limit each
    _write(root / "K3CODE.md", "top\n" + "\n".join(f"@part{i}.md" for i in range(5)) + "\nTAIL")
    text = load_memory(root)[0].text
    assert len(text) <= memory.MAX_MEMORY_TOTAL + 1  # one past the cap, so the prompt can tell it was cut
    assert "P0" in text and "P1" not in text and "TAIL" not in text
    prompt = memory_prompt(root)
    assert len(prompt) < memory.MAX_MEMORY_TOTAL + 1_000 and "…(truncated)" in prompt


def test_multibyte_text_keeps_the_character_cap(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / "K3CODE.md", "é" * 30_000)  # two bytes a character: the cap counts characters
    prompt = memory_prompt(root)
    assert "é" * 20_000 in prompt and "é" * 20_001 not in prompt


def _cut_prompt(root: Path, text: str, **kw: int) -> str:
    _write(root / "K3CODE.md", text)
    return memory_prompt(root, **kw)


def test_a_file_cut_at_whitespace_keeps_its_truncation_marker(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    # the 20,001st character is a newline: the cut text ends in whitespace, but a file longer than the cap is cut
    prompt = _cut_prompt(root, "q" * 20_000 + "\n" + "b" * 100)
    assert prompt.count("q") == 20_000 and "b" not in prompt and "…(truncated)" in prompt
    prompt = _cut_prompt(root, "q" * 19_990 + "\n" * 30 + "tail text that the model must be told is missing")
    assert prompt.count("q") == 19_990 and "tail text" not in prompt and "…(truncated)" in prompt
    prompt = _cut_prompt(root, "q" * 100 + "\n" + "b" * 100, limit=100)  # the same with a smaller per-file cap
    assert "b" not in prompt and "…(truncated)" in prompt
    prompt = _cut_prompt(root, "q" * 20_001 + "b")  # a letter there: always had the marker
    assert prompt.count("q") == 20_000 and "…(truncated)" in prompt


def test_leading_whitespace_does_not_use_up_the_cap(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    prompt = _cut_prompt(root, "\n" * 100 + "  " + "q" * 25_000)
    assert prompt.count("q") == 20_000 and "…(truncated)" in prompt
    assert "…(truncated)" not in _cut_prompt(root, "\n" * 100_000 + "q" * 10)  # whitespace only is stripped, not cut


def test_whitespace_after_the_cap_is_not_a_cut(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    prompt = _cut_prompt(root, "q" * 20_000 + "\n" * 300_000)  # a long blank tail loses nothing
    assert prompt.count("q") == 20_000 and "…(truncated)" not in prompt
    prompt = _cut_prompt(root, "q" * 20_000 + "\n" * 300_000 + "z")  # unless something follows it
    assert prompt.count("q") == 20_000 and "z" not in prompt and "…(truncated)" in prompt


def _reference_prompt(root: Path, limit: int, total: int) -> str:
    """The memory prompt as it was before files were read only as far as the caps use them: every file and import
    read whole, the text stripped, then cut."""
    seen: set[str] = set()
    parts: list[str] = []
    chain = [root, *(d for d in (root / "pkg",) if d.is_dir())]
    paths = [p / name for p in chain for name in memory.PROJECT_FILES if (p / name).is_file()]
    texts = []
    for path in paths:
        text = _reference_expand(path, (root,), ()).strip()
        if text and text not in seen:
            seen.add(text)
            texts.append((path, text))
    left = total
    bodies: dict[int, str] = {}
    # the budget goes to the files nearest the working directory first; the prompt keeps the outermost-first order
    for i in sorted(range(len(texts)), key=lambda i: -len(texts[i][0].relative_to(root).parts)):
        text = texts[i][1]
        body = text if len(text) <= limit else text[:limit] + "\n…(truncated)"
        if left <= 0:
            continue
        if len(body) > left:
            body = body[:left] + "\n…(truncated)"
        left -= len(body)
        bodies[i] = body
    for i, (path, _text) in enumerate(texts):
        if i not in bodies:
            continue
        name = path.relative_to(root).as_posix()
        parts.append(
            f"## Project memory ({name})\n\n" + memory.fenced("project instructions (from the repository):", bodies[i])
        )
    if len(bodies) < len(texts):
        parts.append(memory.TOTAL_MARKER.format(n=len(texts) - len(bodies), cap=total))
    return "\n\n".join(parts)


def _reference_expand(path: Path, roots: tuple[Path, ...], stack: tuple[Path, ...]) -> str:
    resolved = path.resolve()
    stack = (*stack, resolved)
    out: list[str] = []
    in_fence = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if memory._FENCE_LINE.match(line):
            in_fence = not in_fence
        m = None if in_fence else memory._IMPORT.match(line.strip())
        if m is None:
            out.append(line)
            continue
        target = memory._import_target(m.group(1), resolved.parent)
        if not target.is_file():
            out.append(line)
        elif target in stack:
            out.append(f"({line.strip()} not imported: it imports itself)")
        elif len(stack) > memory.MAX_IMPORT_DEPTH:
            out.append(f"({line.strip()} not imported: imports nest at most {memory.MAX_IMPORT_DEPTH} deep)")
        else:
            out.append(_reference_expand(target, roots, stack).rstrip("\n"))
    return "\n".join(out)


def test_files_that_agree_up_to_the_cap_are_listed_once(tmp_path: Path) -> None:
    """Known difference from reading everything: only the part the prompt can show is compared for duplicates."""
    root = _repo(tmp_path)
    _write(root / "K3CODE.md", "s" * 20_001 + "one")
    _write(root / "AGENTS.md", "s" * 20_001 + "two")
    assert [m.path.name for m in load_memory(root, max_chars=20_000)] == ["K3CODE.md"]


def _random_text(rng, size: int) -> str:
    """Words, blank lines, indented lines, whitespace runs, multibyte characters and a few @import lines."""
    pieces: list[str] = []
    n = 0
    while n < size:
        piece = rng.choice(
            [
                "word " * rng.randint(1, 30),
                "\n",
                "\n\n\n",
                "   \n",
                "\t",
                "  indented line\n",
                "é" * rng.randint(1, 40),
                "x" * rng.randint(1, 3000),
                "\r\n",
                "\u3000",
                "@sub/a.md\n",
                "@sub/b.md\n",
                "@missing.md\n",
                "```\n@sub/a.md\n```\n",
            ]
        )
        pieces.append(piece)
        n += len(piece)
    return "".join(pieces)[:size]


def test_the_prompt_is_the_same_as_reading_everything_when_the_files_fit_or_are_cut_at_whitespace(
    tmp_path: Path,
) -> None:
    import random

    rng = random.Random(118)
    caps = [(20_000, 40_000), (100, 40_000), (500, 700), (60, 40_000), (3_000, 5_000), (20_000, 20_000)]
    checked = 0
    for case in range(30):
        root = _repo(tmp_path / f"case{case}")
        for name in ("a", "b"):
            _write(root / "sub" / f"{name}.md", _random_text(rng, rng.choice([0, 5, 400, 5_000, 30_000])))
        (root / "pkg").mkdir()
        for name in memory.PROJECT_FILES:
            if rng.random() < 0.6:
                body = f"{name}\n" + _random_text(rng, rng.choice([0, 30, 90, 101, 500, 6_000, 21_000, 45_000]))
                _write(root / name, body)
        if rng.random() < 0.5:  # a file whose cut falls on whitespace: the shape of the original defect
            limit, _ = rng.choice(caps)
            _write(root / "AGENTS.md", "q" * limit + rng.choice(["\n", " ", "\n\n  ", "\u3000"]) + "r" * 50)
        _write(root / "pkg" / "K3CODE.md", "pkg " + _random_text(rng, rng.choice([10, 700, 9_000])))
        for limit, total in caps:
            got = memory_prompt(root / "pkg", limit=limit, total=total)
            want = _reference_prompt(root, limit, total)
            assert got == want, (case, limit, total)
            checked += 1
    assert checked == 180


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


# A session whose project root is $HOME reads ~/AGENTS.md, which the model can write (an in-roots edit): an @import
# there must not pull a home credential store into the prompt. Fake HOME (the conftest fixture) and fake values.
_FAKE_TOKEN = "fake-" + "token-for-test"


def _home_credentials() -> dict[str, Path]:
    home = Path.home()
    return {
        "@.npmrc": _write(home / ".npmrc", "//registry/:_authToken=" + _FAKE_TOKEN),
        "@.config/gh/hosts.yml": _write(home / ".config/gh/hosts.yml", "oauth_token: " + _FAKE_TOKEN),
        "@.local/share/uv/credentials/credentials.toml": _write(
            home / ".local/share/uv/credentials/credentials.toml", "password = '" + _FAKE_TOKEN + "'"
        ),
        "@.config/git/credentials": _write(home / ".config/git/credentials", "https://u:" + _FAKE_TOKEN + "@h"),
    }


def test_home_rooted_project_memory_cannot_import_a_home_credential_store(monkeypatch) -> None:
    home = Path.home()
    monkeypatch.setenv("K3CODE_HOME", str(home / ".k3code"))
    imports = _home_credentials()
    _write(home / "notes.md", "ORDINARY-NOTES")
    _write(home / "AGENTS.md", "head\n" + "\n".join(imports) + "\n@notes.md")
    text = " ".join(m.text for m in load_memory(home))
    assert _FAKE_TOKEN not in text and "ORDINARY-NOTES" in text
    assert text.count("not imported: outside the allowed directories") == len(imports)
    assert _FAKE_TOKEN not in memory.memory_prompt(home)


def test_home_rooted_project_memory_file_symlinked_to_a_credential_store_is_refused(monkeypatch) -> None:
    home = Path.home()
    monkeypatch.setenv("K3CODE_HOME", str(home / ".k3code"))
    creds = _home_credentials()
    (home / "AGENTS.md").symlink_to(creds["@.npmrc"])
    (home / "K3CODE.md").symlink_to(creds["@.config/git/credentials"])
    (home / "CLAUDE.md").symlink_to(creds["@.config/gh/hosts.yml"])
    assert load_memory(home) == []
    assert _FAKE_TOKEN not in memory.memory_prompt(home)


def test_an_import_through_a_symlink_into_a_credential_store_is_refused(monkeypatch) -> None:
    home = Path.home()
    monkeypatch.setenv("K3CODE_HOME", str(home / ".k3code"))
    _home_credentials()
    (home / "innocent.txt").symlink_to(home / ".npmrc")
    _write(home / "AGENTS.md", "@innocent.txt")
    assert _FAKE_TOKEN not in " ".join(m.text for m in load_memory(home))


def test_user_memory_cannot_import_a_home_credential_store(monkeypatch) -> None:
    home = Path.home()
    k3home = home / ".k3code"
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    _home_credentials()
    _write(home / "prefs.md", "MY-PREFS")
    _write(k3home / "memory" / "other.md", "OWN-STATE")
    _write(k3home / "memory" / "USER.md", "@~/.npmrc\n@~/.config/gh/hosts.yml\n@~/prefs.md\n@other.md")
    text = load_memory(home / "nowhere")[0].text
    assert _FAKE_TOKEN not in text and "MY-PREFS" in text and "OWN-STATE" in text


def test_a_repository_below_home_keeps_its_dot_folders_importable(tmp_path: Path, monkeypatch) -> None:
    home = Path.home()
    monkeypatch.setenv("K3CODE_HOME", str(home / ".k3code"))
    root = home / "src" / "proj"
    (root / ".git").mkdir(parents=True)
    _write(root / ".github" / "contributing.md", "CI-RULES")
    _write(root / "AGENTS.md", "@.github/contributing.md")
    assert "CI-RULES" in load_memory(root)[0].text
