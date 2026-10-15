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


def _decisions(home: Path, *rows: tuple[str, str]) -> None:
    """Rows of the decision log: ``(cwd, choice)`` each, filed under ``project_id(cwd)`` as the hub files them."""
    from k3code.learning.decisions import DecisionLog

    log = DecisionLog(home)
    for cwd, choice in rows:
        log.record("approval", session="s1", cwd=cwd, subject="pytest *", choice=choice)
    log.record("auto_apply", subject="terse overlay", choice="applied", actor="auto")  # the optimizer's: no project
    log.close()


def _rows(home: Path) -> list[tuple[str, str]]:
    from k3code.learning.decisions import DecisionLog

    log = DecisionLog(home)
    try:
        return [(r["cwd"], r["choice"]) for r in log.query(actor=None)]
    finally:
        log.close()


def test_learned_purge_deletes_this_projects_decision_rows_only(tmp_path, monkeypatch):
    """/learned purge left decisions.db alone (only the yearly age prune ever deleted from it), so the distiller
    learned the same preferences again from the project's decisions. It now deletes the rows of this project: those
    under its id and those recorded anywhere in it (a subdirectory has an id of its own when there is no remote)."""
    home, cwd, run, _ = _setup(tmp_path, monkeypatch)
    (cwd / ".git").mkdir()
    (cwd / "pkg").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _decisions(home, (str(cwd), "allow"), (str(cwd / "pkg"), "allow"), (str(other), "deny"))

    assert "decision log rows (learning/decisions.db)" in run("purge")  # the prompt says what goes
    assert len(_rows(home)) == 4  # negative gate: nothing without --yes
    out = run("purge --yes")
    assert "2 decision log row(s) of this project" in out
    assert _rows(home) == [(str(other), "deny"), ("", "applied")]  # another project's and the optimizer's stay


def test_learned_purge_does_not_create_a_decision_log(tmp_path, monkeypatch):
    """A context without a learning hub opens the log itself, and makes none that is missing."""
    home, _cwd, run, _ = _setup(tmp_path, monkeypatch)
    assert "0 decision log row(s)" in run("purge --yes")
    assert not (home / "learning" / "decisions.db").exists()


def test_learned_purge_all_clears_every_project_and_preferences_but_keeps_confirmed_items(tmp_path, monkeypatch):
    """``--all`` also clears every project's decisions and the learned preferences (preferences.json and the auto
    section of USER.md); accepted gotchas, the machine gotchas and the lines the user wrote in USER.md stay."""
    from k3code.learning import distiller, gotchas
    from k3code.learning.decisions import project_id

    home, cwd, run, collect = _setup(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    _decisions(home, (str(cwd), "allow"), (str(other), "deny"))
    gotchas.append_gotcha(gotchas.user_gotchas_path(home), "machine: use gmake")
    (home / "decisions.jsonl.migrated").write_text('{"tool": "bash"}\n', encoding="utf-8")
    prefs = [distiller.Preference("use tabs", 0.9, 5, "a"), distiller.Preference("be terse", 0.8, 4, "b")]
    user_md = user_memory_path()
    user_md.parent.mkdir(parents=True, exist_ok=True)
    user_md.write_text("# Me\n- I wrote this myself\n", encoding="utf-8")
    distiller.write_preferences_json(home, prefs)
    distiller.write_auto_section(user_md, prefs)
    user_md.write_text(user_md.read_text(encoding="utf-8") + "- added after the block\n", encoding="utf-8")

    prompt = run("purge --all")
    assert "every project's decision log rows" in prompt and "--all --yes" in prompt
    assert len(_rows(home)) == 3 and distiller.load_preferences(home)  # negative gate: nothing without --yes
    out = run("purge --all --yes")
    assert "3 decision log row(s)" in out and "2 learned preference(s)" in out
    assert _rows(home) == [] and not (home / "decisions.jsonl.migrated").exists()
    assert not (home / "learning" / "preferences.json").exists()
    assert distiller.read_auto_section(user_md) == []
    text = user_md.read_text(encoding="utf-8")
    assert "- I wrote this myself" in text and "- added after the block" in text  # the user's lines stay
    assert "use tabs" not in text and "be terse" not in text
    # what the user confirmed stays: the accepted project gotcha and the machine gotcha
    assert gotchas.gotchas_path(project_id(cwd), home).read_text(encoding="utf-8").count("never run make clean") == 1
    kinds = {i.kind for i in collect(cwd)}
    assert kinds == {"gotcha", "machine gotcha"}


def test_learned_purge_all_leaves_a_user_md_without_a_learned_block_alone(tmp_path, monkeypatch):
    home, _cwd, run, _ = _setup(tmp_path, monkeypatch)
    user_md = user_memory_path()
    user_md.parent.mkdir(parents=True, exist_ok=True)
    user_md.write_text("- only mine\n", encoding="utf-8")
    assert "0 learned preference(s)" in run("purge --all --yes")
    assert user_md.read_text(encoding="utf-8") == "- only mine\n"


def test_learned_help_and_usage_say_what_each_purge_removes(tmp_path, monkeypatch):
    from k3code.commands.learned_cmd import LearnedCommand

    _home, _cwd, run, _ = _setup(tmp_path, monkeypatch)
    help_text = LearnedCommand().help
    assert help_text.splitlines()[0].endswith("/learned purge [--all] --yes")  # /help shows the first line
    for needle in ("gotchas.auto.md", "learned.md", "learning/decisions.db", "overlay not in an active experiment"):
        assert needle in help_text.splitlines()[1]
    assert "preferences.json" in help_text.splitlines()[2] and "every project's" in help_text.splitlines()[2]
    assert "accepted gotchas" in help_text.splitlines()[3] and "USER.md" in help_text.splitlines()[3]
    assert "--all goes with purge only" in run("forget x --all")
    assert "purge [--all] --yes" in run("bogus")
