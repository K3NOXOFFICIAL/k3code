"""The deterministic permission floor: every row of ``fixtures/permission_floor_corpus.jsonl`` is decided per mode as
its class says. The corpus is the single source of truth; rows are named by their line number, never by their text.

- hardline: denied with ``Decision.hardline`` set in every mode, yolo and headless included.
- needs_human: ask + needs_human in default and auto (never auto-allowed), denied headless, allowed in yolo (the owner's
  explicit no-prompt mode).
- stop: hardline or needs_human; never allowed in default or auto.
- allow: allowed in auto without a prompt inside the project (no over-blocking).
"""

from __future__ import annotations

import json
import random
import shlex
from pathlib import Path

import pytest

from k3code.permissions import decide

_CORPUS = Path(__file__).parent / "fixtures" / "permission_floor_corpus.jsonl"
ROWS: list[tuple[int, dict]] = [
    (n, json.loads(line)) for n, line in enumerate(_CORPUS.read_text().splitlines(), 1) if line.strip()
]
_RANK = {"allow": 0, "ask": 1, "deny": 2}


def _ids(rows: list[tuple[int, dict]]) -> list[str]:
    return [f"row{n}" for n, _ in rows]


def _of(*classes: str) -> list[tuple[int, dict]]:
    return [(n, r) for n, r in ROWS if r["class"] in classes]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project next to a FAKE home (never the real one)."""
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return proj


def _decide(cmd: str, mode: str, cwd: Path, *, headless: bool = False):
    return decide(mode=mode, tool="bash", args={"command": cmd}, cwd=cwd, headless=headless)


@pytest.mark.parametrize(("n", "row"), _of("hardline"), ids=_ids(_of("hardline")))
def test_hardline_rows_are_denied_in_every_mode(project: Path, n: int, row: dict) -> None:
    for mode, headless in (("default", False), ("auto", False), ("auto", True), ("yolo", False), ("plan", False)):
        d = _decide(row["cmd"], mode, project, headless=headless)
        assert d.action == "deny" and d.hardline, f"row {n} {mode} headless={headless}: {d.action}"


@pytest.mark.parametrize(("n", "row"), _of("needs_human"), ids=_ids(_of("needs_human")))
def test_needs_human_rows_ask_a_person(project: Path, n: int, row: dict) -> None:
    for mode in ("default", "auto"):
        d = _decide(row["cmd"], mode, project)
        assert (d.action, d.needs_human, d.auto_allowed) == ("ask", True, False), f"row {n} {mode}: {d.action}"
    assert _decide(row["cmd"], "auto", project, headless=True).action == "deny", f"row {n} headless"
    assert _decide(row["cmd"], "yolo", project).action == "allow", f"row {n} yolo"


@pytest.mark.parametrize(("n", "row"), _of("stop"), ids=_ids(_of("stop")))
def test_stop_rows_are_never_allowed(project: Path, n: int, row: dict) -> None:
    for mode in ("default", "auto"):
        d = _decide(row["cmd"], mode, project)
        floor = (d.action == "deny" and d.hardline) or (d.action == "ask" and d.needs_human)
        assert floor and not d.auto_allowed, f"row {n} {mode}: {d.action}"
    assert _decide(row["cmd"], "auto", project, headless=True).action == "deny", f"row {n} headless"


@pytest.mark.parametrize(("n", "row"), _of("allow"), ids=_ids(_of("allow")))
def test_allow_rows_run_in_auto_without_a_prompt(project: Path, n: int, row: dict) -> None:
    d = _decide(row["cmd"], "auto", project)
    assert d.action == "allow" and not d.hardline and not d.needs_human, f"row {n}: {d.action}"


# ── properties over the whole corpus ──


@pytest.mark.parametrize(("n", "row"), ROWS, ids=_ids(ROWS))
def test_a_hardline_in_one_mode_is_a_hardline_in_all(project: Path, n: int, row: dict) -> None:
    decisions = {m: _decide(row["cmd"], m, project) for m in ("default", "auto", "yolo", "plan")}
    if any(d.hardline for d in decisions.values()):
        assert all(d.action == "deny" and d.hardline for d in decisions.values()), f"row {n}"


@pytest.mark.parametrize(("n", "row"), ROWS, ids=_ids(ROWS))
def test_severity_is_monotone_over_the_modes(project: Path, n: int, row: dict) -> None:
    ranks = [_RANK[_decide(row["cmd"], m, project).action] for m in ("default", "auto", "yolo")]
    assert ranks[0] >= ranks[1] >= ranks[2], f"row {n}: {ranks}"


_WRAPPERS = [
    lambda c: f"sudo {c}",
    lambda c: f"env X=1 {c}",
    lambda c: f"time {c}",
    lambda c: f"nice {c}",
    lambda c: f"bash -c {shlex.quote(c)}",
    lambda c: 'sh -c "' + c.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"',
    lambda c: f"( {c} )",
    lambda c: f"{{ {c}; }}",
    lambda c: f"if true; then {c}; fi",
]
_FLOOR = _of("hardline", "needs_human", "stop")
#: Indexes in _WRAPPERS of the ones that prefix only the first command of a line (sudo, env, nice): an assignment they
#: wrap is not an assignment of the shell any more.
_PREFIX_WRAPPERS = frozenset({0, 1, 3})


def _starts_with_assignment(cmd: str) -> bool:
    first = cmd.split(None, 1)[0] if cmd.strip() else ""
    return "=" in first and first.split("=", 1)[0].isidentifier()


@pytest.mark.parametrize(("n", "row"), _FLOOR, ids=_ids(_FLOOR))
def test_a_wrapper_never_lowers_the_decision(project: Path, n: int, row: dict) -> None:
    rng = random.Random(n)  # a fixed order per row: a failure names the same wrapper every run
    wrappers = list(enumerate(_WRAPPERS))
    rng.shuffle(wrappers)
    assigns_first = _starts_with_assignment(row["cmd"])
    for mode in ("default", "auto"):
        bare = _RANK[_decide(row["cmd"], mode, project).action]
        for w, wrap in wrappers:
            if assigns_first and w in _PREFIX_WRAPPERS:
                continue  # `sudo X=/; rm -rf $X` never sets X for the rm: a different, harmless-looking line
            got = _RANK[_decide(wrap(row["cmd"]), mode, project).action]
            assert got >= bare, f"row {n} {mode} wrapper {w}: {got} < {bare}"
