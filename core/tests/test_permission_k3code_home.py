"""k3code's own state (``$K3CODE_HOME``) is never changed by the edit tools without a person (K3NOX-716).

The trust store, ``memory/USER.md``, user skills, output styles and overlays decide what the next session loads and what
its prompt says. ``write`` and ``edit`` run in the daemon, outside the sandbox, and an in-roots write is allowed in auto
mode (a session rooted at ``$HOME`` covers the state directory) and in yolo mode, so the model could plant a trust
record or standing instructions. Writing anywhere under the directory now asks in every mode, like ``.k3code/config*``,
whatever the directory is named. k3code's own writers do not go through ``decide``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from k3code.permissions import Rule, decide, engine

MODES = ["default", "accept-edits", "auto", "yolo"]
STATE_FILES = [
    "trusted_projects.json",
    "memory/USER.md",
    "skills/x/SKILL.md",
    "output-styles/terse.md",
    "overlays/o1.json",
    "sessions/s1/journal.jsonl",
]


def _home() -> Path:
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    return home


def _symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):  # pragma: no cover - no symlinks here
        pytest.skip("symlinks are not available")


@pytest.fixture(params=[".k3code", "state/k3-data"], ids=["default-name", "other-name"])
def state(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The state directory, inside the fake home, named as k3code names it or not."""
    d = _home() / request.param
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("K3CODE_HOME", str(d))
    return d


def _asks(mode: str, tool: str, path: str, cwd: Path, **kw) -> bool:
    dec = decide(mode=mode, tool=tool, args={"path": path}, cwd=cwd, **kw)
    return dec.action == "ask" and dec.needs_human and dec.always_ask and not dec.auto_allowed


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize("rel", STATE_FILES)
def test_a_session_rooted_at_home_asks_for_the_state_directory_in_every_mode(state: Path, tool: str, rel: str) -> None:
    for mode in MODES:
        assert _asks(mode, tool, str(state / rel), _home()), (mode, rel)
        assert _asks(mode, tool, os.path.relpath(state / rel, _home()), _home()), (mode, rel)
        headless = decide(mode=mode, tool=tool, args={"path": str(state / rel)}, cwd=_home(), headless=True)
        assert headless.action == "deny", (mode, rel)
    assert decide(mode="plan", tool=tool, args={"path": str(state / rel)}, cwd=_home()).action == "deny"
    assert engine.protected_path(str(state / rel))


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize("rel", STATE_FILES)
def test_a_repository_session_never_gets_an_allow_for_the_state_directory(
    state: Path, tmp_path: Path, tool: str, rel: str
) -> None:
    proj = _home() / "src" / "proj"
    proj.mkdir(parents=True)
    for mode in MODES:
        dec = decide(mode=mode, tool=tool, args={"path": str(state / rel)}, cwd=proj)
        assert dec.action != "allow", (mode, rel, dec)
        if mode != "auto":  # auto refuses a write outside the roots outright, as before
            assert dec.action == "ask" and dec.always_ask, (mode, rel, dec)
    assert decide(mode="auto", tool=tool, args={"path": str(state / rel)}, cwd=proj).action == "deny"


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_no_allow_rule_turns_the_ask_back_into_an_allow(state: Path, tool: str) -> None:
    path = str(state / "trusted_projects.json")
    for pattern in ("*", f"{_home()}/*", f"{state}/*", path):
        rule = Rule("edit", pattern, "allow")
        for layer in ("user_rules", "project_rules", "session_rules"):
            for mode in ("auto", "yolo"):
                assert _asks(mode, tool, path, _home(), **{layer: [rule]}), (mode, layer, pattern)
    deny = Rule("edit", f"{state}/*", "deny")
    assert decide(mode="yolo", tool=tool, args={"path": path}, cwd=_home(), user_rules=[deny]).action == "deny"


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_a_symlink_does_not_hide_the_state_directory(state: Path, tool: str) -> None:
    (state / "memory").mkdir(exist_ok=True)
    (state / "memory" / "USER.md").write_text("notes\n")
    (state / "trusted_projects.json").write_text("{}\n")
    _symlink(_home() / "work" / "notes.md", state / "memory" / "USER.md")  # a link to a file
    _symlink(_home() / "work" / "mem", state / "memory")  # a link to a directory
    _symlink(_home() / "work" / "ghost", state / "skills" / "new" / "SKILL.md")  # a file that does not exist yet
    for mode in MODES:
        for rel in ("work/notes.md", "work/mem/USER.md", "work/mem/new.md", "work/ghost"):
            assert _asks(mode, tool, rel, _home()), (mode, rel)
    # `..` after a link is applied to the link's target
    _symlink(_home() / "work" / "up", state / "memory")
    for mode in MODES:
        assert _asks(mode, tool, "work/up/../trusted_projects.json", _home()), mode


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_a_state_directory_reached_through_a_link_is_protected_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    real = _home() / "real-state"
    real.mkdir()
    link = _home() / "link-state"
    _symlink(link, real)
    for spelled in (link, real):  # K3CODE_HOME names the link or the target; a path may use either
        monkeypatch.setenv("K3CODE_HOME", str(spelled))
        for path in (link / "memory" / "USER.md", real / "memory" / "USER.md", link / "trusted_projects.json"):
            for mode in MODES:
                assert _asks(mode, tool, str(path), _home()), (spelled, path, mode)


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_a_state_directory_set_with_a_tilde_or_a_dotdot_is_protected(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    (_home() / "data").mkdir(exist_ok=True)
    for spelling in ("~/data", str(_home() / "a" / ".." / "data")):
        monkeypatch.setenv("K3CODE_HOME", spelling)
        for mode in MODES:
            assert _asks(mode, tool, str(_home() / "data" / "trusted_projects.json"), _home()), (spelling, mode)


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_other_files_keep_their_decisions(state: Path, tmp_path: Path, tool: str) -> None:
    home = _home()
    sibling = home / f"{state.name}-other"  # shares the directory's name as a prefix, is not inside it
    sibling.mkdir(parents=True, exist_ok=True)
    for path in (home / "notes.txt", home / "proj" / "a.py", sibling / "x.json", home / "skills" / "x" / "SKILL.md"):
        assert decide(mode="auto", tool=tool, args={"path": str(path)}, cwd=home).action == "allow", path
        assert decide(mode="yolo", tool=tool, args={"path": str(path)}, cwd=home).action == "allow", path
        assert decide(mode="default", tool=tool, args={"path": str(path)}, cwd=home).action == "ask", path
    proj = tmp_path / "proj"
    proj.mkdir()
    for mode in ("accept-edits", "auto", "yolo"):
        assert decide(mode=mode, tool=tool, args={"path": "memory/USER.md"}, cwd=proj).action == "allow", mode


def test_only_edits_are_affected_and_the_parent_directory_is_not_protected(state: Path) -> None:
    assert not engine.protected_path(str(_home() / "notes.txt"))
    assert not engine.protected_path(str(state.parent / "elsewhere.txt"))
    # a read is decided as before: the protection is for the edit tools (a read of the memory file in a $HOME-rooted
    # session already asks as a dot-folder read, or is allowed, never made an always-ask by this rule)
    dec = decide(mode="auto", tool="read", args={"path": str(state / "memory" / "USER.md")}, cwd=_home())
    assert "runs as code" not in (dec.message or "")
