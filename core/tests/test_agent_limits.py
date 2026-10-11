"""The agent-limits resolver, its config keys and their validation, the governor width and the agent budget."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from k3code.autonomy import autonomy_cfg
from k3code.autonomy.agents import (
    AUTO_CEILING,
    HARD_MAX_PARALLEL,
    agent_limits,
    clean_override,
    session_limits,
)
from k3code.config import Settings, load_config
from k3code.reliability.governor import Governor
from k3code.subagents.budget import AgentBudget, BudgetStop


def cfg(**kw):
    return Settings(**kw)


def test_defaults_are_todays_numbers():
    lim = agent_limits(cfg())
    assert (lim.max_agents, lim.auto, lim.dynamic) == (12, False, True)
    assert (lim.max_parallel, lim.max_nested, lim.max_spawns, lim.fanout_parallel) == (4, 3, 16, 3)
    assert lim.fanout_width == 3  # the fan-out's own ceiling is lower than the session's
    assert set(lim.sources.values()) == {"default"}
    assert lim.budget_agents == 12


def test_config_sets_each_limit_and_the_source_says_so():
    lim = agent_limits(
        cfg(
            ultracode={"max_agents": 20},
            autonomy={"agents": {"dynamic": False, "max_parallel": 6, "max_nested": 0, "max_spawns": 0}},
        )
    )
    assert (lim.max_agents, lim.dynamic, lim.max_parallel, lim.max_nested, lim.max_spawns) == (20, False, 6, 0, 0)
    assert lim.sources["max_agents"] == lim.sources["max_parallel"] == "config"
    assert agent_limits(cfg(autonomy={"fanout": {"max_parallel": 5}})).fanout_width == 4  # bounded by max_parallel


def test_a_session_override_beats_the_config_field_by_field():
    config = cfg(ultracode={"max_agents": 20}, autonomy={"agents": {"max_parallel": 6}})
    lim = agent_limits(config, {"max_agents": 30, "dynamic": False})
    assert (lim.max_agents, lim.dynamic, lim.max_parallel) == (30, False, 6)
    assert lim.sources == {
        "max_agents": "session",
        "dynamic": "session",
        "max_parallel": "config",
        "max_nested": "default",
        "max_spawns": "default",
    }


def test_auto_starts_at_the_ceiling_and_has_no_fixed_count():
    lim = agent_limits(cfg(ultracode={"max_agents": "auto"}))
    assert lim.auto and lim.max_agents is None and lim.budget_agents == AUTO_CEILING
    assert lim.describe_max_agents() == "auto"
    assert agent_limits(cfg(), {"max_agents": " AUTO "}).auto  # the session can ask for it too


@pytest.mark.parametrize(
    "override",
    [
        {"max_agents": 0},
        {"max_agents": True},
        {"max_agents": "abc"},
        {"max_agents": 65},
        {"max_parallel": 9},
        {"dynamic": "on"},
    ],
)
def test_a_bad_override_counts_as_unset(override):
    config = cfg(ultracode={"max_agents": 7})
    assert agent_limits(config, override) == agent_limits(config)
    assert clean_override(override) == {}


def test_clean_override_keeps_only_what_is_valid():
    assert clean_override({"max_agents": "auto", "max_parallel": 2, "dynamic": False, "junk": 1}) == {
        "max_agents": "auto",
        "max_parallel": 2,
        "dynamic": False,
    }
    assert clean_override(None) == {}


def test_session_limits_reads_the_sessions_meta():
    session = SimpleNamespace(stored=SimpleNamespace(meta={"agent_limits": {"max_agents": 5, "max_parallel": 99}}))
    lim = session_limits(cfg(), session)
    assert lim.max_agents == 5 and lim.max_parallel == 4  # 99 is out of range: the default stays
    assert session_limits(cfg(), SimpleNamespace()).max_agents == 12


@pytest.mark.parametrize("value", [0, -1, True, "abc", 65, 2.5, None])
def test_ultracode_max_agents_is_validated_when_the_config_loads(value):
    with pytest.raises(ValidationError, match=r"ultracode.max_agents must be a whole number from 1 to 64"):
        Settings(ultracode={"max_agents": value})


@pytest.mark.parametrize("ok", [1, 12, 64, "auto", "AUTO", 3.0])
def test_ultracode_max_agents_accepts_numbers_and_auto(ok):
    assert Settings(ultracode={"max_agents": ok}).ultracode == {"max_agents": ok}  # the section is stored as written


@pytest.mark.parametrize("value", [0, -5, True, "x"])
def test_ultracode_max_tokens_is_validated(value):
    with pytest.raises(ValidationError, match=r"ultracode.max_tokens must be a whole number"):
        Settings(ultracode={"max_tokens": value})


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"dynamic": "yes"}, r"autonomy.agents.dynamic must be true or false"),
        ({"max_parallel": 0}, r"autonomy.agents.max_parallel must be a whole number from 1 to 8"),
        ({"max_parallel": 9}, r"autonomy.agents.max_parallel must be a whole number from 1 to 8"),
        ({"max_nested": -1}, r"autonomy.agents.max_nested must be a whole number from 0 to 8"),
        ({"max_spawns": -1}, r"autonomy.agents.max_spawns must be a whole number 0 or more"),
        ({"max_spawns": "many"}, r"autonomy.agents.max_spawns"),
    ],
)
def test_autonomy_agents_values_are_validated(section, message):
    with pytest.raises(ValidationError, match=message):
        Settings(autonomy={"agents": section})


def test_autonomy_agents_must_be_a_mapping_and_edge_values_load():
    with pytest.raises(ValidationError, match=r"autonomy.agents must be a mapping"):
        Settings(autonomy={"agents": 4})
    edge = {"max_parallel": HARD_MAX_PARALLEL, "max_nested": 0, "max_spawns": 0, "dynamic": False}
    assert Settings(autonomy={"agents": edge}).autonomy["agents"] == edge


def test_the_agents_section_merges_with_the_defaults():
    assert autonomy_cfg(cfg(autonomy={"agents": {"max_parallel": 6}}))["agents"] == {
        "dynamic": True,
        "max_parallel": 6,
        "max_nested": 3,
        "max_spawns": 16,
    }


def test_unknown_agents_keys_and_a_bad_fanout_width_are_warned_about(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        json.dumps({"autonomy": {"agents": {"max_parallell": 3}, "fanout": {"max_parallel": 50}}})
    )
    with caplog.at_level(logging.WARNING, logger="k3code.config"):
        load_config()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "autonomy.agents.max_parallell is not used" in text
    assert "autonomy.fanout.max_parallel 50 is not a whole number from 1 to 8" in text


@pytest.mark.parametrize(("configured", "width"), [("x", 3), (0, 3), (50, 4), (2, 2)])
def test_a_bad_fanout_width_still_loads_and_resolves_to_a_usable_number(configured, width):
    assert agent_limits(cfg(autonomy={"fanout": {"max_parallel": configured}})).fanout_width == width


def test_spend_raising_keys_are_listed_for_a_project_config_to_flag():
    from k3code.trust import SECURITY_KEYS

    keys = {k for k, _ in SECURITY_KEYS}
    assert {"ultracode.max_agents", "ultracode.max_tokens", "autonomy.agents"} <= keys


def test_the_governor_cap_honours_a_ceiling_and_keeps_the_io_bound():
    gov = Governor()
    assert gov.agent_cap(9) == 3  # no ceiling: the governor's own default
    assert gov.agent_cap(9, ceiling=4) == 4
    assert gov.agent_cap(2, ceiling=4) == 2
    assert gov.agent_cap(9, ceiling=HARD_MAX_PARALLEL) == 8
    assert gov.agent_cap(9, ceiling=4, io_heavy=True) == 2


def test_the_budget_holds_back_the_review_tail():
    b = AgentBudget(max_agents=9)
    b.reserve(3)
    for _ in range(6):
        b.take_agent()
    assert b.left() == 0 and b.left(reserved=True) == 3
    with pytest.raises(BudgetStop, match=r"6/9 agents, 3 held back for the review") as stop:
        b.take_agent()
    assert stop.value.kind == "agents"
    for _ in range(3):
        b.take_agent(reserved=True)  # the lenses and the fixer may use what was held back
    with pytest.raises(BudgetStop, match=r"9/9 agents"):
        b.take_agent(reserved=True)


def test_a_token_stop_is_marked_as_one_and_trim_never_raises_the_cap():
    b = AgentBudget(max_agents=24, max_tokens=10)
    b.agents, b.tokens = 3, 10
    with pytest.raises(BudgetStop) as stop:
        b.take_agent(reserved=True)
    assert stop.value.kind == "tokens"
    b.trim(12)
    assert b.max_agents == 12
    b.trim(50)
    assert b.max_agents == 12
    b.trim(1)
    assert b.max_agents == 3  # never below what was already taken
