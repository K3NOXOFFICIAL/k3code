"""Issue #141: ``learning.enabled: false`` keeps learned blocks out of the prompt, and ``/learned`` lists, forgets and
purges what learning stored."""

from __future__ import annotations

from pathlib import Path

from k3code import prompting
from k3code.config import Settings
from k3code.memory import user_memory_path, write_learned


def _seed_prompt_state(home: Path, cwd: Path) -> None:
    user_memory_path().parent.mkdir(parents=True, exist_ok=True)
    user_memory_path().write_text("- I write my own notes\n", encoding="utf-8")
    write_learned(cwd, ["build with make all"])
    (home / "overlays").mkdir(parents=True)
    (home / "overlays" / "x1-terse.md").write_text("answer tersely", encoding="utf-8")


def test_learning_off_leaves_learned_notes_and_overlays_out_of_the_prompt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("K3CODE_HOME", str(home))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _seed_prompt_state(home, cwd)

    on = dict(prompting.prompt_sections("base", cwd=cwd, config=Settings()))
    assert "build with make all" in on["memory"] and "answer tersely" in on["overlay"]

    off = dict(prompting.prompt_sections("base", cwd=cwd, config=Settings(learning={"enabled": False})))
    assert "build with make all" not in off["memory"] and "Learned project notes" not in off["memory"]
    assert "overlay" not in off
    assert "I write my own notes" in off["memory"]  # the user's own memory is not learned


def _setup(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from k3code.commands.learned_cmd import LearnedCommand, collect
    from k3code.learning import gotchas
    from k3code.learning.decisions import project_id
    from k3code.learning.optimizer import Experiments

    home = tmp_path / "home"
    monkeypatch.setenv("K3CODE_HOME", str(home))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    pid = project_id(cwd)
    gotchas.append_gotcha(gotchas.gotchas_path(pid, home), "never run make clean")
    gotchas.append_gotcha(gotchas.auto_gotchas_path(pid, home), "pytest needs -x")
    write_learned(cwd, ["build with make all"])
    (home / "overlays").mkdir(parents=True)
    (home / "overlays" / "x1-terse.md").write_text("answer tersely", encoding="utf-8")
    ctx = SimpleNamespace(learning=SimpleNamespace(experiments=Experiments(home)), sessions={})
    cmd = LearnedCommand()

    def run(arg):
        return asyncio.run(cmd.handle(ctx, None, arg))["message"]

    return home, cwd, run, collect


def test_learned_lists_every_kind_with_ids(tmp_path, monkeypatch):
    _home, _cwd, run, _ = _setup(tmp_path, monkeypatch)
    out = run("")
    for needle in ("gotcha ", "auto gotcha", "note", "overlay", "never run make clean", "build with make all"):
        assert needle in out


def test_learned_forget_drops_only_that_line(tmp_path, monkeypatch):
    _home, cwd, run, collect = _setup(tmp_path, monkeypatch)
    items = collect(cwd)
    note = next(i for i in items if i.kind == "note")
    assert "Forgot" in run(f"forget {note.id}")
    kinds = {i.kind for i in collect(cwd)}
    assert "note" not in kinds and {"gotcha", "auto gotcha", "overlay"} <= kinds


def test_learned_forget_unknown_id_changes_nothing(tmp_path, monkeypatch):
    _home, cwd, run, collect = _setup(tmp_path, monkeypatch)
    before = collect(cwd)
    assert "No learned item" in run("forget deadbeef")
    assert collect(cwd) == before


def test_learned_forget_overlay_in_experiment_rolls_back(tmp_path, monkeypatch):
    home, cwd, run, collect = _setup(tmp_path, monkeypatch)
    import json

    (home / "learning").mkdir(exist_ok=True)
    (home / "learning" / "experiments.json").write_text(
        json.dumps([{"id": "x1", "name": "terse", "kind": "overlay", "status": "active"}]), encoding="utf-8"
    )
    ov = next(i for i in collect(cwd) if i.kind == "overlay")
    assert "rolled back" in run(f"forget {ov.id}")
    assert not (home / "overlays" / "x1-terse.md").exists()
    assert json.loads((home / "learning" / "experiments.json").read_text())[0]["status"] == "rolled_back"


def test_learned_purge_requires_yes_and_spares_active_overlays(tmp_path, monkeypatch):
    import json

    home, cwd, run, collect = _setup(tmp_path, monkeypatch)
    (home / "overlays" / "x2-other.md").write_text("keep", encoding="utf-8")
    (home / "learning").mkdir(exist_ok=True)
    (home / "learning" / "experiments.json").write_text(
        json.dumps([{"id": "x2", "name": "other", "kind": "overlay", "status": "active"}]), encoding="utf-8"
    )
    before = collect(cwd)
    assert "--yes" in run("purge")
    assert collect(cwd) == before  # negative gate: nothing without --yes
    assert "Purged" in run("purge -y")
    left = {i.kind for i in collect(cwd)}
    assert "auto gotcha" not in left and "note" not in left and "gotcha" in left
    assert (home / "overlays" / "x2-other.md").exists() and not (home / "overlays" / "x1-terse.md").exists()


def test_learned_forget_preference_leaves_json_and_user_md(tmp_path, monkeypatch):
    from k3code.learning import distiller

    home, cwd, run, collect = _setup(tmp_path, monkeypatch)
    prefs = [distiller.Preference("use tabs", 0.9, 5, "a"), distiller.Preference("be terse", 0.8, 4, "b")]
    distiller.write_preferences_json(home, prefs)
    distiller.write_auto_section(user_memory_path(), prefs)
    pref = next(i for i in collect(cwd) if i.text == "use tabs")
    assert "learn it again" in run(f"forget {pref.id}")
    assert [p.text for p in distiller.load_preferences(home)] == ["be terse"]
    assert distiller.read_auto_section(user_memory_path()) == ["be terse"]
