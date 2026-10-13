"""Re-running a setup step must not destroy what the user (or another k3code command) put in USER.md or
config.yaml: USER.md is only touched inside its marked block, and a step writes only the keys it owns."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from k3code import memory
from k3code.cli import cli
from k3code.learning import distiller
from k3code.paths import user_config_path
from k3code.setup import state as st
from k3code.setup.prompter import AnswerPrompter
from k3code.setup.steps import write_user_md
from k3code.setup.wizard import run_setup

GW = {"preset": "omniroute", "name": "gw", "base_url": "http://127.0.0.1:9/v1", "api_key_env": "GW_KEY"}
ANSWERS: dict[str, Any] = {
    "welcome": {"mode": "fresh"},
    "about": {"name": "tester", "language": "de", "experience": "expert", "verbosity": "terse"},
    "system": {"confirm": True},
    "usage": {"primary": "ops", "languages": [], "frameworks": []},
    "providers": {"entries": [GW, {"preset": "anthropic"}], "test": False},
    "tiers": {"main": "m-main", "strong": "m-strong", "cheap": "m-cheap", "fast": "m-fast"},
    "permissions": {"mode": "ask", "extra_hardline": ["ssh \\S+ systemctl"]},
    "integrations": {"mcp": [], "mem0_url": "", "skills_roots": [], "searxng_url": ""},
    "theme": {"theme": "midnight", "focus_mode": True, "keymap": "tuios"},
    "service": {"install": False},
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / ".k3code"))
    monkeypatch.setenv("K3CODE_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    for k in ("GW_KEY", "OMNIROUTE_API_KEY", "ANTHROPIC_API_KEY", "K3CODE_API_KEY"):
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    return tmp_path


def _cfg() -> dict[str, Any]:
    return yaml.safe_load(user_config_path().read_text())


def _put_cfg(cfg: dict[str, Any]) -> None:
    user_config_path().write_text(yaml.safe_dump(cfg))


def _step(step: str, answers: dict[str, Any] | None = None) -> None:
    run_setup(AnswerPrompter({**ANSWERS, **(answers or {})}), only_step=step, do_probe=False)


def _fast(root: Path, endpoint: str) -> None:
    path = root / "fast.yaml"
    path.write_text(
        yaml.safe_dump({"onboard": {"mode": "fast", "provider": "api", "endpoint": endpoint, "key": "k", "model": "m"}})
    )
    r = CliRunner().invoke(cli, ["onboard", "--answers", str(path), "--no-probe"])
    assert r.exit_code == 0, r.output


def _user_md() -> Path:
    return memory.user_memory_path()


def _backups() -> list[Path]:
    return list(_user_md().parent.glob("USER.md.bak-*"))


# ---- #92: USER.md ----------------------------------------------------------------------------------------------


def test_a_step_rerun_keeps_notes_and_learned_preferences_in_user_md(env: Path) -> None:
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)
    memory.append_memory(_user_md(), "always answer in German")
    pref = distiller.Preference(key="k", text="prefers tabs", confidence=0.8, evidence=3)
    distiller.write_auto_section(_user_md(), [pref])
    _step("system", {"system": {"confirm": True, "shell": "fish"}})
    text = _user_md().read_text()
    assert "- always answer in German" in text
    assert "- prefers tabs" in text and distiller.HEADING in text
    assert "shell fish" in text and "tester" in text
    assert text.count("# About the user") == 1 and text.count("k3code:setup begin") == 1
    for step in ("about", "usage"):
        _step(step)
    assert "- always answer in German" in _user_md().read_text()
    run_setup(AnswerPrompter(ANSWERS), restart=True, do_probe=False)  # the summary step of a second full run
    assert "- always answer in German" in _user_md().read_text()


def test_the_setup_block_is_replaced_in_place_and_rerunning_is_idempotent(env: Path) -> None:
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)
    memory.append_memory(_user_md(), "keep me")
    _step("about")
    first = _user_md().read_text()
    _step("about")
    assert _user_md().read_text() == first
    _step("about", {"about": {"name": "renamed"}})
    text = _user_md().read_text()
    assert "renamed" in text and "tester" not in text and "- keep me" in text
    assert _backups() == []  # a block replace needs no backup


def test_a_user_md_from_an_older_setup_is_backed_up_and_its_notes_kept(env: Path) -> None:
    _user_md().parent.mkdir(parents=True)
    _user_md().write_text(
        "# About the user\n\n- Name/handle: old\n- Language: en; experience: ; verbosity: \n"
        "- Primary use: ; languages: ; frameworks: \n- System: , shell , editor , toolchains , tailscale False\n"
        "- I prefer tabs\n\n" + distiller.HEADING + "\n- learned one\n"
    )
    said: list[str] = []
    write_user_md({"about": {"name": "kk"}}, said.append)
    text = _user_md().read_text()
    assert "- I prefer tabs" in text and "- learned one" in text
    assert "Name/handle: kk" in text and "Name/handle: old" not in text
    (bak,) = _backups()
    assert "Name/handle: old" in bak.read_text() and "- I prefer tabs" in bak.read_text()
    assert len(said) == 1 and bak.name in said[0]  # the user is told where the copy went
    write_user_md({"about": {"name": "kk"}})
    assert _user_md().read_text() == text and len(_backups()) == 1


def test_the_issue_repro_keeps_the_memory_line(env: Path) -> None:
    write_user_md({"about": {"name": "kk"}})
    memory.append_memory(_user_md(), "keep me")
    write_user_md({"about": {"name": "kk"}})
    assert "keep me" in _user_md().read_text()


def test_answers_cannot_break_out_of_the_setup_block(env: Path) -> None:
    write_user_md({"about": {"name": "x -->\n- injected\n<!-- k3code:setup end -->"}})
    memory.append_memory(_user_md(), "keep me")
    assert _user_md().read_text().count("<!-- k3code:setup end -->") == 1
    write_user_md({"about": {"name": "y"}})
    text = _user_md().read_text()
    assert "injected" not in text and "- keep me" in text


# ---- #93: providers and the other config keys -------------------------------------------------------------------


def test_tiers_after_a_fast_onboarding_keeps_the_provider_chain(env: Path) -> None:
    _fast(env, "http://127.0.0.1:9/v1")
    before = _cfg()["providers"]
    assert [p["name"] for p in before] == ["endpoint"]
    _step("tiers", {"tiers": {"main": "x"}})
    after = _cfg()["providers"]
    assert [p["name"] for p in after] == ["endpoint"]
    assert {k: v for k, v in after[0].items() if k != "models"} == {k: v for k, v in before[0].items() if k != "models"}
    assert after[0]["models"] == {"default": "x"}


def test_tiers_after_a_changed_chain_uses_the_chain_in_config_yaml_not_the_saved_state(env: Path) -> None:
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)  # state says: gw, anthropic
    _fast(env, "http://127.0.0.1:19/v1")  # chain is now: endpoint, gw, anthropic
    assert [p["name"] for p in _cfg()["providers"]] == ["endpoint", "gw", "anthropic"]
    _step("tiers", {"tiers": {"main": "x"}})
    assert [p["name"] for p in _cfg()["providers"]] == ["endpoint", "gw", "anthropic"]
    assert all(p["models"] == {"default": "x"} for p in _cfg()["providers"])
    assert [e["name"] for e in st.load_state()["data"]["providers"]["entries"]] == ["gw", "anthropic"]


def test_tiers_with_no_providers_anywhere_writes_none(env: Path) -> None:
    _step("tiers")
    assert not _cfg().get("providers")


def test_tiers_rerun_is_idempotent_and_keeps_extra_provider_keys(env: Path) -> None:
    _fast(env, "http://127.0.0.1:9/v1")
    cfg = _cfg()
    cfg["providers"][0]["descriptions"] = {"default": "Everyday"}
    cfg["providers"][0]["thinking_tokens"] = 5
    _put_cfg(cfg)
    _step("tiers", {"tiers": {"main": "x", "cheap": "c"}})
    first = _cfg()
    assert first["providers"][0]["descriptions"] == {"default": "Everyday"}
    assert first["providers"][0]["thinking_tokens"] == 5
    _step("tiers", {"tiers": {"main": "x", "cheap": "c"}})
    assert _cfg() == first


def test_the_providers_step_keeps_extra_keys_of_an_entry_it_does_not_ask_about(env: Path) -> None:
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)
    cfg = _cfg()
    cfg["providers"][0]["descriptions"] = {"default": "Everyday"}
    _put_cfg(cfg)
    _step("providers")
    assert _cfg()["providers"][0]["descriptions"] == {"default": "Everyday"}
    assert _cfg()["providers"][0]["models"]["cheap"] == "m-cheap"


def test_a_step_rerun_writes_only_the_keys_it_owns(env: Path) -> None:
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)
    cfg = _cfg()
    cfg["display"]["density"] = "compact"  # TUI display settings the wizard never asks about
    cfg["autonomy"]["review"] = {"gate": True}
    cfg["autonomy"]["fanout"]["floor"] = 1
    cfg["permissions"]["bash"] = {"git status*": "allow"}
    cfg["tiers"]["main"] = "strong"
    cfg["panes"]["border"] = "round"
    _put_cfg(cfg)
    _step("theme", {"theme": {"theme": "mono", "focus_mode": False, "keymap": "k3"}})
    _step("usage", {"usage": {"primary": "research", "languages": [], "frameworks": []}})
    _step("permissions", {"permissions": {"mode": "yolo", "extra_hardline": []}})
    now = _cfg()
    assert now["display"] == {"theme": "mono", "focus_mode": False, "density": "compact"}
    assert now["panes"] == {"keymap": "k3", "border": "round"}
    assert now["autonomy"]["review"] == {"gate": True}
    assert now["autonomy"]["plan_first"] is False and now["autonomy"]["fanout"] == {"max_parallel": 6, "floor": 1}
    assert now["permission_mode"] == "yolo"
    assert now["permissions"] == {"bash": {"git status*": "allow"}}  # hardline cleared, the rest kept
    assert now["tiers"]["main"] == "strong"  # no step asks about the tier map


def test_a_full_run_over_an_existing_config_keeps_keys_it_does_not_ask_about(env: Path) -> None:
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)
    cfg = _cfg()
    cfg["display"]["density"] = "compact"
    cfg["permissions"]["bash"] = {"git status*": "allow"}
    _put_cfg(cfg)
    run_setup(AnswerPrompter(ANSWERS), restart=True, do_probe=False)
    assert _cfg()["display"]["density"] == "compact"
    assert _cfg()["permissions"]["bash"] == {"git status*": "allow"}


def test_summary_after_a_fast_onboarding_keeps_the_tier_models(env: Path) -> None:
    _fast(env, "http://127.0.0.1:9/v1")
    before = _cfg()
    assert before["providers"][0]["models"] == {"default": "m"}
    _step("summary")
    assert _cfg()["providers"] == before["providers"]


def test_summary_keeps_hand_edited_tier_models_over_the_saved_answers(env: Path) -> None:
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)  # state says: gw has m-main, m-strong, ...
    cfg = _cfg()
    cfg["providers"][0]["models"] = {"default": "hand", "cheap": "hand-cheap"}
    _put_cfg(cfg)
    _step("summary")
    assert _cfg()["providers"][0]["models"] == {"default": "hand", "cheap": "hand-cheap"}


def test_summary_alone_leaves_a_hand_edited_config_as_it_is(env: Path) -> None:
    run_setup(AnswerPrompter(ANSWERS), do_probe=False)  # the saved answers say: theme midnight
    cfg = _cfg()
    cfg["display"]["theme"] = "nord"
    _put_cfg(cfg)
    user_config_path().chmod(0o600)
    before = user_config_path().read_text()
    _step("summary")
    assert user_config_path().read_text() == before
    assert (user_config_path().stat().st_mode & 0o777, _user_md().stat().st_mode & 0o777) == (0o600, 0o600)


def test_summary_after_a_partial_run_writes_no_keys_of_steps_that_never_ran(env: Path) -> None:
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    _put_cfg({"display": {"theme": "dracula"}})
    _step("about")
    _step("tiers")  # the saved answers now hold about and tiers only
    before = user_config_path().read_text()
    _step("summary")
    assert user_config_path().read_text() == before
    cfg = _cfg()
    assert cfg["display"] == {"theme": "dracula"}
    assert not {"permission_mode", "output_style", "autonomy", "panes"} & cfg.keys()


def test_tiers_with_a_blank_answer_keeps_the_models_an_entry_has(env: Path) -> None:
    _fast(env, "http://127.0.0.1:9/v1")
    cfg = _cfg()
    cfg["providers"][0]["models"] = {"default": "b-main", "strong": "a-strong"}
    _put_cfg(cfg)
    _step("tiers", {"tiers": {}})  # Enter on every tier prompt, or no tiers in the answers file
    assert _cfg()["providers"][0]["models"] == {"default": "b-main", "strong": "a-strong"}
