"""/maxagents, and /ultracode agents|dynamic as its aliases: the switches for the sub-agent limits."""

from __future__ import annotations

import pytest

from k3code import confio
from k3code.autonomy.agents import agent_limits, session_limits
from k3code.bundle import _UNTRUSTED_META
from k3code.commands.agent_limits_cmd import AgentLimitsError, alias_args, parse
from k3code.paths import user_config_path
from m1cmd_helpers import cmd, make_server, new_session, rpc

# ── the words ─────────────────────────────────────────────────────────


def test_the_words_a_user_types_become_settings():
    assert parse([]) == {} and parse(["status"]) == {}
    assert parse(["20"]) == {"max_agents": 20}
    assert parse(["AUTO"]) == {"max_agents": "auto"}
    assert parse(["parallel", "6"]) == {"max_parallel": 6}
    assert parse(["dynamic", "off"]) == {"dynamic": False}
    assert parse(["dynamic", "on"]) == {"dynamic": True}
    assert parse(["dynamic", "status"]) == {}
    assert parse(["reset"]) is None


@pytest.mark.parametrize(
    "args",
    [
        ["0"],
        ["65"],
        ["-3"],
        ["many"],
        ["2.5"],
        ["parallel", "9"],
        ["parallel", "0"],
        ["parallel"],
        ["dynamic", "maybe"],
    ],
)
def test_a_number_out_of_range_or_an_unknown_word_is_refused(args):
    with pytest.raises(AgentLimitsError):
        parse(args)


def test_the_aliases_match_the_whole_argument_only():
    assert alias_args("agents 20") == "20"
    assert alias_args("agents auto") == "auto"
    assert alias_args("agents") == "" and alias_args("agents status") == "status"
    assert alias_args("agents reset --save") == "reset --save"
    assert alias_args("dynamic off") == "dynamic off" and alias_args("dynamic") == "dynamic status"
    # anything else is a task for the pipeline, even when it starts with the word
    for task in (
        "agents should review the diff",
        "agents 20 and more",
        "dynamic dispatch in the parser",
        "dynamic maybe",
        "fix the agents",
        "",
    ):
        assert alias_args(task) is None, task


# ── the command ───────────────────────────────────────────────────────


async def seed(tmp_path, monkeypatch, **settings):
    server, _ = make_server(tmp_path, monkeypatch, ["unused"], **settings)
    sid = await new_session(server, tmp_path)
    return server, server.live[sid]


async def test_status_shows_each_limit_and_where_it_came_from(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, ultracode={"max_agents": 20})
    res = await cmd(server, "/maxagents", live.session_id)
    out = res["output"]
    assert "max agents per /ultracode run: 20 (config)" in out
    assert "dynamic sizing: on" in out and "running at once: 4" in out
    assert "a fan-out runs min(3, 4) = 3" in out
    assert res["agent_limits"] == {
        "max_agents": 20,
        "dynamic": True,
        "max_parallel": 4,
        "max_nested": 3,
        "max_spawns": 16,
        "fanout_width": 3,
        "sources": res["agent_limits"]["sources"],
        "live": 0,
    }
    assert res["agent_limits"]["sources"]["max_agents"] == "config"
    await server.close()


async def test_a_setting_is_for_this_session_unless_it_is_saved(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch)
    res = await cmd(server, "/maxagents 30", live.session_id)
    assert "For this session only (add --save to keep it)" in res["output"]
    assert "max agents per /ultracode run: 30 (session)" in res["output"]
    assert live.stored.meta["agent_limits"] == {"max_agents": 30}
    assert not user_config_path().exists()
    assert session_limits(server.config, live).max_agents == 30 and agent_limits(server.config).max_agents == 12
    await cmd(server, "/maxagents parallel 6", live.session_id)
    await cmd(server, "/maxagents dynamic off", live.session_id)
    assert live.stored.meta["agent_limits"] == {"max_agents": 30, "max_parallel": 6, "dynamic": False}
    info = server.live[live.session_id].live_info()
    assert info["agent_limits"] == {"max_agents": 30, "max_parallel": 6, "dynamic": False}
    await cmd(server, "/maxagents reset", live.session_id)
    assert "agent_limits" not in live.stored.meta
    assert "max agents per /ultracode run: 12" in (await cmd(server, "/maxagents", live.session_id))["output"]
    await server.close()


async def test_save_writes_the_user_config_and_applies_at_once(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch)
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text("ultracode:\n  max_tokens: 500000\nmodels: {}\n")
    res = await cmd(server, "/maxagents 24 --save", live.session_id)
    assert "Saved in" in res["output"]
    data = confio.read_yaml(user_config_path())
    assert data["ultracode"] == {"max_tokens": 500000, "max_agents": 24}  # the other key survives
    assert server.config.ultracode["max_agents"] == 24  # the running gateway took it
    await cmd(server, "/maxagents parallel 6 --save", live.session_id)
    await cmd(server, "/maxagents dynamic off --save", live.session_id)
    assert confio.read_yaml(user_config_path())["autonomy"]["agents"] == {"max_parallel": 6, "dynamic": False}
    got = agent_limits(server.config)
    assert (got.max_agents, got.max_parallel, got.dynamic) == (24, 6, False)
    await server.close()


async def test_saving_a_setting_replaces_the_sessions_own_copy_of_it(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch)
    await cmd(server, "/maxagents 30", live.session_id)
    await cmd(server, "/maxagents parallel 2", live.session_id)
    await cmd(server, "/maxagents 18 --save", live.session_id)
    assert live.stored.meta["agent_limits"] == {"max_parallel": 2}  # max_agents now follows the saved value
    assert session_limits(server.config, live).max_agents == 18
    await server.close()


async def test_a_saved_value_a_project_config_hides_is_called_out(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch)
    server.config.ultracode = {"max_agents": 5}  # what a project's `ultracode:` section leaves in the shared config
    monkeypatch.setattr(server, "apply_file_config", lambda cwd=None: None)  # the overlay stays
    res = await cmd(server, "/maxagents 24 --save", live.session_id)
    assert "replaces that whole section" in res["output"]
    assert confio.read_yaml(user_config_path())["ultracode"] == {"max_agents": 24}  # written; the overlay still wins
    assert agent_limits(server.config).max_agents == 5
    await server.close()


async def test_auto_is_stored_and_shown(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch)
    res = await cmd(server, "/maxagents auto", live.session_id)
    assert live.stored.meta["agent_limits"] == {"max_agents": "auto"}
    assert "max agents per /ultracode run: auto up to 24, trimmed to what the plan needs (session)" in res["output"]
    assert res["agent_limits"]["max_agents"] == "auto"
    await server.close()


async def test_bad_input_prints_the_usage_and_changes_nothing(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch)
    for line in ("/maxagents 0", "/maxagents 99", "/maxagents parallel 9", "/maxagents lots", "/maxagents --bogus"):
        out = (await cmd(server, line, live.session_id))["output"]
        assert "Usage: /maxagents" in out, line
    assert "agent_limits" not in live.stored.meta and not user_config_path().exists()
    await server.close()
    bare, _ = make_server(tmp_path / "bare", monkeypatch)  # no session in this gateway
    assert "No active session" in (await cmd(bare, "/maxagents 20"))["output"]
    assert "No active session" in (await cmd(bare, "/maxagents reset"))["output"]
    assert "Sub-agent limits" in (await cmd(bare, "/maxagents"))["output"]  # status needs no session
    await bare.close()


async def test_the_ultracode_aliases_run_the_same_handler_and_a_task_still_runs_as_a_task(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch)
    res = await cmd(server, "/ultracode agents 15", live.session_id)
    assert live.stored.meta["agent_limits"] == {"max_agents": 15} and "agent_limits" in res
    await cmd(server, "/ultracode dynamic off", live.session_id)
    assert live.stored.meta["agent_limits"]["dynamic"] is False
    assert "max agents per /ultracode run: 15" in (await cmd(server, "/ultracode agents", live.session_id))["output"]
    started = []
    monkeypatch.setattr(server, "start_job", lambda live_, label, factory: started.append(label))
    await cmd(server, "/ultracode agents should review the diff", live.session_id)
    assert started == ["/ultracode agents should review the diff"]
    await server.close()


async def test_status_in_the_agents_overlay_reports_what_runs(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch, autonomy={"fanout": {"max_parallel": 5}})
    res = (await rpc(server, "delegation.status", {}))["result"]
    assert res["max_concurrent_children"] == 4  # the fan-out's 5, held to the session's parallel limit of 4
    assert res["max_spawn_depth"] == 2 and res["dynamic"] is True and res["max_agents"] == 12
    assert res["live"] == {"children": 0, "queued": 0}
    await cmd(server, "/maxagents parallel 2", live.session_id)
    res = (await rpc(server, "delegation.status", {"session_id": live.session_id}))["result"]
    assert res["max_concurrent_children"] == 2 and res["max_parallel"] == 2
    await server.close()


def test_an_imported_bundle_cannot_carry_a_raised_limit():
    assert "agent_limits" in _UNTRUSTED_META


async def test_a_fork_and_a_background_session_inherit_the_limits(tmp_path, monkeypatch):
    server, live = await seed(tmp_path, monkeypatch)
    await cmd(server, "/maxagents 33", live.session_id)
    child = server._fresh_session_like(live, background=True)
    assert child.stored.meta["agent_limits"] == {"max_agents": 33}
    await server.close()
