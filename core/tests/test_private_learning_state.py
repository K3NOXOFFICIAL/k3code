"""The state the learning loop and the proposal store keep under $K3CODE_HOME is 0600/0700 whatever the umask.

It holds what the owner approved, what failed and what worked in their projects, and the prompts' verdicts: the same
kind of content as ``decisions.db`` and ``learned.md``, which were already private. The service unit runs with the
user's umask (0022), so a writer that does not set its own mode leaves the file world-readable."""

from __future__ import annotations

import contextlib
import stat
from pathlib import Path
from types import SimpleNamespace

from k3code import paths
from k3code.autonomy.proposals import ProposalStore
from k3code.autonomy.scope import ScopeLog, ScopeVerdict
from k3code.learning import curator, gotchas, projectstate, review
from k3code.learning.hub import LearningHub
from k3code.learning.optimizer import Experiments
from k3code.learning.replay import ReplayStore
from test_private_state import mode, umask

PID = "hygiene-project"


def write_all(home: Path, project: Path) -> list[str]:
    """Run every writer of learning and proposal state once; returns the paths (relative to ``home``) they made."""
    ProposalStore(home).add("note", "prefers uv", "remember it")
    log = ScopeLog(home)
    log.outcome(log.verdict("fix the bug", ScopeVerdict(scope="small", needs_plan=False)), "done")
    assert ReplayStore(home).append({"session": "s1", "turn": 1})
    gotchas.append_gotcha(gotchas.gotchas_path(PID, home), "pytest needs uv run")
    gotchas.append_gotcha(gotchas.auto_gotchas_path(PID, home), "rg is missing here")
    baseline = {"escalation_rate": 0.0, "loop_guard_per_session": 0.0, "approval_prompts_per_session": 0.0}
    Experiments(home).start({"title": "t", "name": "terse", "prompt": "be brief"}, baseline)
    hub = LearningHub(SimpleNamespace(_home=lambda: home, config=None), ProposalStore(home))
    hub._set_state(distilled=1.0)
    with contextlib.suppress(AttributeError):
        hub.log.close()
    projectstate.save(project, {"version": projectstate.VERSION})
    projectstate.add_mcp_server(project, "docs", {"command": "docs-mcp"})
    curator.record_use("terse")
    review.write_draft("tidy", "tidy up", "body")
    curator.curate(ProposalStore(home))
    return sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file())


def loose_modes(home: Path, *, dirs: bool = True) -> list[str]:
    """Every file under ``home`` that is not 0600 and (unless ``dirs`` is false) every directory that is not 0700."""
    bad = []
    for p in sorted(home.rglob("*")):
        want = 0o700 if p.is_dir() else 0o600
        if (dirs or p.is_file()) and mode(p) != want:
            bad.append(f"{p.relative_to(home)}: {stat.filemode(p.stat().st_mode)}")
    return bad


def project_dir(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    return project


def test_learning_and_proposal_state_is_0600_and_0700_whatever_the_umask(tmp_path: Path) -> None:
    home = paths.home()
    with umask(0):  # the loosest umask: every mode below is set explicitly, not inherited
        made = write_all(home, project_dir(tmp_path))
    assert {
        "proposals.jsonl",
        "scope_log.jsonl",
        "learning/state.json",
        "learning/experiments.json",
        "learning/replay/turns.jsonl",
        "overlays/x1-terse.md",
        f"projects/{gotchas.project_dir_name(PID)}/gotchas.md",
        f"projects/{gotchas.project_dir_name(PID)}/gotchas.auto.md",
        "skills/.usage.json",
        "skills/.curator.json",
        "skills/_drafts/tidy/SKILL.md",
    } <= set(made), made  # each writer ran
    assert any(m.endswith("/project.json") for m in made) and any(m.endswith("/mcp.json") for m in made), made
    assert loose_modes(home) == []


def test_older_loose_learning_files_are_tightened(tmp_path: Path, monkeypatch) -> None:
    home = paths.home()
    project = project_dir(tmp_path)
    with umask(0):
        monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "scratch"))
        made = [m for m in write_all(tmp_path / "scratch", project) if "decisions.db" not in m]
        monkeypatch.setenv("K3CODE_HOME", str(home))
        for rel in made:  # what an older daemon (umask 0022) left behind
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            (home / rel).touch()
            (home / rel).chmod(0o644)
        for d in home.rglob("*"):
            if d.is_dir():
                d.chmod(0o755)
        write_all(home, project)
    assert loose_modes(home, dirs=False) == []


def test_ensure_private_dir_makes_every_missing_ancestor_0700(tmp_path: Path) -> None:
    kept = tmp_path / "kept"
    kept.mkdir(mode=0o755)
    kept.chmod(0o755)
    with umask(0):
        paths.ensure_private_dir(kept / "a" / "b")
        paths.private_file(kept / "c" / "d" / "f.txt")
    assert [mode(kept / x) for x in ("a", "a/b", "c", "c/d")] == [0o700] * 4
    assert mode(kept / "c" / "d" / "f.txt") == 0o600
    assert mode(kept) == 0o755  # an existing ancestor is not ours to change
