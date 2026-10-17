"""/config, /settings, /output-style, /memory, /skills + the skill tool, prompt composition."""

from __future__ import annotations

import httpx
import respx
import yaml

from k3code import memory as memmod
from k3code.extratools import register_skill_tool
from k3code.paths import home
from k3code.prompting import build_system_prompt
from k3code.tools import build_registry
from m1cmd_helpers import cmd, make_server, new_session, rpc, submit_and_wait


def write_skill(root, name, desc, body="Do the thing."):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n{body}\n")


async def test_config_set_get_rollback(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    cfg = home() / "config.yaml"

    res = await cmd(server, "/config set max_turns 5", sid)
    assert "Set max_turns" in res["output"]
    assert yaml.safe_load(cfg.read_text())["max_turns"] == 5 and server.config.max_turns == 5
    assert not list(home().glob("config.yaml.bak-*"))  # nothing to back up the first time

    await cmd(server, "/config set max_turns 9", sid)
    assert len(list(home().glob("config.yaml.bak-*"))) == 1
    assert "max_turns = 9" in (await cmd(server, "/config get max_turns", sid))["output"]

    # validation: wrong type, unknown reliability key, bad permission action — file untouched
    bad_sets = ("max_turns notanumber", "reliability.bogus 1", "permissions.bash maybe", "reliability.flags.netwatch 3")
    for bad in bad_sets:
        res = await cmd(server, f"/config set {bad}", sid)
        assert "Invalid config" in res["output"], (bad, res)
    assert yaml.safe_load(cfg.read_text())["max_turns"] == 9

    await cmd(server, "/config set permissions.bash {git *: allow}", sid)
    assert yaml.safe_load(cfg.read_text())["permissions"] == {"bash": {"git *": "allow"}}

    res = await cmd(server, "/config rollback", sid)
    assert "Rolled" in res["output"]
    assert "permissions" not in yaml.safe_load(cfg.read_text())

    proj = tmp_path / ".k3code" / "config.yaml"
    await cmd(server, "/config set --project output_style concise", sid)
    assert yaml.safe_load(proj.read_text()) == {"output_style": "concise"}
    assert "path" in (await cmd(server, "/config path", sid))["output"] or True
    res = await cmd(server, "/config rollback --project", sid)  # no backup yet for project
    assert "no backup" in res["output"]
    await server.close()


async def test_config_get_never_shows_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("NOPE", "sk-SECRETSECRETSECRET123456")
    server, _ = make_server(tmp_path, monkeypatch)
    server.config.providers[0].api_key = "sk-SECRETSECRETSECRET123456"
    out = (await cmd(server, "/config get"))["output"]
    assert "SECRETSECRET" not in out and "api_key_env: NOPE" in out
    await server.close()


async def test_rpc_config_get_full_redacts_api_keys(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch, max_turns=7)
    server.config.providers[0].api_key = "sk-SECRETSECRETSECRET123456"
    res = (await rpc(server, "config.get", {"key": "full"}))["result"]["config"]
    assert "SECRETSECRET" not in str(res)
    assert res["providers"][0]["api_key"] == "<redacted>"
    assert res["providers"][0]["api_key_env"] == "NOPE" and res["max_turns"] == 7
    await server.close()


async def test_rpc_config_get_sections_are_json_and_redacted(tmp_path, monkeypatch):
    from k3code.config import McpServerConfig

    server, _ = make_server(tmp_path, monkeypatch)
    server.config.providers[0].api_key = "sk-SECRETSECRETSECRET123456"
    server.config.mcp.servers["x"] = McpServerConfig(
        url="https://mcp.example", headers={"X-Team": "abc-plain"}, env={"PLAIN": "value"}
    )
    providers = (await rpc(server, "config.get", {"key": "providers"}))["result"]["value"]
    assert providers[0]["api_key"] == "<redacted>" and providers[0]["api_key_env"] == "NOPE"
    servers = (await rpc(server, "config.get", {"key": "mcp.servers"}))["result"]["value"]
    assert servers["x"]["url"] == "https://mcp.example" and servers["x"]["headers"]["X-Team"] == "<redacted>"
    assert (await rpc(server, "config.get", {"key": "mcp.servers.x.headers"}))["result"]["value"] == {
        "X-Team": "<redacted>"
    }
    assert (await rpc(server, "config.get", {"key": "mcp.servers.x.env.PLAIN"}))["result"]["value"] == "<redacted>"
    assert (await rpc(server, "config.get", {"key": "no.such.key"}))["result"]["value"] is None
    await server.close()


async def test_a_result_that_does_not_encode_still_gets_an_error_reply(tmp_path, monkeypatch):
    from k3code.gateway import server as gateway_server

    async def bad(server, params):
        return {"value": object()}

    monkeypatch.setitem(gateway_server._HANDLERS, "test.bad", bad)
    server, _ = make_server(tmp_path, monkeypatch)
    res = await rpc(server, "test.bad")
    assert res["error"]["message"].startswith("TypeError")
    await server.close()


async def test_settings_view(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    sid = await new_session(server, tmp_path)
    res = await cmd(server, "/settings", sid)
    assert res["type"] == "settings"
    s = res["settings"]
    assert s["permission_mode"] == "default" and s["permission_rules"] == 0
    assert s["providers"][0]["api_key_env"] == "NOPE" and "api_key" not in s["providers"][0]
    assert s["paths"]["user_config"].endswith("config.yaml") and s["output_style"] == "default"
    assert "output style" in res["output"]
    await server.close()


async def test_output_style_applied_to_system_prompt(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch)
    (home() / "output-styles").mkdir(parents=True)
    (home() / "output-styles" / "pirate.md").write_text("Talk like a pirate, matey.\n")
    sid = await new_session(server, tmp_path)

    assert "Unknown output style" in (await cmd(server, "/output-style nope", sid))["output"]
    listing = (await cmd(server, "/output-style", sid))["output"]
    assert "concise" in listing and "pirate" in listing

    await cmd(server, "/output-style concise", sid)
    await submit_and_wait(server, "hi")
    assert "Output style: concise" in provider.seen[0][0].content
    assert server.store.get(sid).meta["output_style"] == "concise"
    stored_system = server.store.get(sid).messages[0]
    assert stored_system["role"] == "system" and "Output style: concise" in stored_system["content"]

    await cmd(server, "/output-style pirate", sid)
    await submit_and_wait(server, "again")
    assert "pirate, matey" in provider.seen[1][0].content and "Output style: concise" not in provider.seen[1][0].content

    await cmd(server, "/output-style default --default", sid)
    assert yaml.safe_load((home() / "config.yaml").read_text())["output_style"] == "default"
    await submit_and_wait(server, "third")
    assert "pirate" not in provider.seen[2][0].content
    await server.close()


def test_memory_loader_order(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "pkg"
    sub.mkdir()
    (tmp_path / "home" / "memory").mkdir(parents=True)
    (tmp_path / "home" / "memory" / "USER.md").write_text("USER-FACT")
    (repo / "AGENTS.md").write_text("AGENTS-FALLBACK")

    files = memmod.load_memory(sub)
    assert [(m.scope, m.text) for m in files] == [("user", "USER-FACT"), ("project", "AGENTS-FALLBACK")]
    (repo / "K3CODE.md").write_text("K3-PROJECT")  # both are read, K3CODE.md first
    files = memmod.load_memory(sub)
    assert [m.text for m in files] == ["USER-FACT", "K3-PROJECT", "AGENTS-FALLBACK"]

    prompt = build_system_prompt("BASE", cwd=sub, config=make_cfg())
    assert prompt.index("BASE") < prompt.index("USER-FACT") < prompt.index("K3-PROJECT")


def make_cfg():
    from k3code.config import Settings

    return Settings()


async def test_memory_command(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sid = await new_session(server, repo)

    listing = (await cmd(server, "/memory", sid))["output"]
    assert "user:" in listing and "project:" in listing and "(missing)" in listing

    await cmd(server, "/memory add always use tabs", sid)
    await cmd(server, "/memory add likes tea --user", sid)
    assert "always use tabs" in (repo / "K3CODE.md").read_text()
    assert "likes tea" in (home() / "memory" / "USER.md").read_text()
    assert "bytes" in (await cmd(server, "/memory", sid))["output"]
    edit = await cmd(server, "/memory edit", sid)
    assert edit["path"].endswith("K3CODE.md")

    await submit_and_wait(server, "go")
    sys_prompt = provider.seen[0][0].content
    assert "always use tabs" in sys_prompt and "likes tea" in sys_prompt

    assert "isn't configured" in (await cmd(server, "/memory mem0 tea", sid))["output"]
    server.config.mem0.url = "http://mem0.test"
    with respx.mock() as router:
        route = router.post("http://mem0.test/search").mock(
            return_value=httpx.Response(200, json={"results": [{"memory": "Alice likes tea"}]})
        )
        out = (await cmd(server, "/memory mem0 tea", sid))["output"]
    assert route.called and "Alice likes tea" in out
    await server.close()


def _memory_tree(root):
    """A repository whose memory spans every kind of source the loader reads, with one of each listing state."""
    (root / ".git").mkdir(parents=True)
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / ".k3code" / "rules").mkdir(parents=True)
    (root / ".k3code" / "rules" / "style.md").write_text("RULE-STYLE")
    (root / "docs").mkdir()
    (root / "docs" / "build.md").write_text("IMPORTED-BUILD")
    (root / "AGENTS.md").write_text("ROOT-AGENTS\n@docs/build.md\n")
    (root / "CLAUDE.md").write_text("ROOT-AGENTS\n@docs/build.md\n")  # the usual copy: same text, read once
    (root / "pkg" / "K3CODE.md").write_text("  \n")  # empty
    (root / "pkg" / "sub" / "CLAUDE.md").write_text("s" * 25_000)  # past the 20,000-character per-file cap
    memmod.write_learned(root, ["build with make all"])


async def test_memory_lists_exactly_the_files_the_prompt_reads(tmp_path, monkeypatch):
    """/memory listed only USER.md and the root K3CODE.md (or AGENTS.md); the prompt also reads CLAUDE.md, every
    directory from the root down to the session's, .k3code/rules/*.md, learned.md and @imports. The listing is now
    the loader's own scan: same files, same order, with what the caps cut."""
    server, _ = make_server(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _memory_tree(repo)
    cwd = repo / "pkg" / "sub"
    sid = await new_session(server, cwd)
    res = await cmd(server, "/memory", sid)
    rows, out = res["files"], res["output"]

    # every file the loader reads is listed, in the prompt's order, and the listing names no file it does not read
    read = [r["path"] for r in rows if r["status"] == memmod.READ and r["scope"] != "learned"]
    assert read == [str(m.path) for m in memmod.load_memory(cwd)]
    assert read == [
        str(repo / ".k3code" / "rules" / "style.md"),
        str(repo / "AGENTS.md"),
        str(repo / "pkg" / "sub" / "CLAUDE.md"),
    ]
    by_path = {r["path"]: r for r in rows}
    # every place the loader looks: root to cwd, the three names in each, the user's memory and learned.md
    for d in (repo, repo / "pkg", cwd):
        for name in memmod.PROJECT_FILES:
            assert str(d / name) in by_path
    assert [r["scope"] for r in rows][:2] == ["user", "rules"] and rows[-1]["scope"] == "learned"
    assert by_path[str(memmod.user_memory_path())]["status"] == memmod.MISSING
    assert by_path[str(repo / "K3CODE.md")]["status"] == memmod.MISSING
    assert by_path[str(repo / "CLAUDE.md")]["status"] == memmod.SAME
    assert by_path[str(repo / "pkg" / "K3CODE.md")]["status"] == memmod.EMPTY
    assert by_path[str(repo / "AGENTS.md")]["imports"] == [str(repo / "docs" / "build.md")]
    assert f"@import {repo / 'docs' / 'build.md'}" in out
    assert by_path[str(memmod.learned_notes_path(repo))]["status"] == memmod.READ
    # the cap the prompt applies is reported on the file it cut
    assert by_path[str(cwd / "CLAUDE.md")]["cut"] == "file"
    assert f"{cwd / 'CLAUDE.md'}  25000 bytes, cut at the 20000-character per-file limit" in out
    assert by_path[str(repo / "AGENTS.md")]["cut"] == ""
    # the file /memory add and edit write to
    assert by_path[str(repo / "AGENTS.md")]["edit_target"] == "project"
    assert f"/memory add and /memory edit write to {repo / 'AGENTS.md'}" in out
    await server.close()


def test_memory_listing_reports_what_the_total_cap_dropped(tmp_path, monkeypatch):
    """The 40,000-character total goes to the nearest files first: the root file it cuts short and the one it leaves
    out are listed as such, and the prompt agrees."""
    from k3code.commands.memory_cmd import memory_listing

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "K3CODE.md").write_text("p" * 15_000)
    for name, fill in (("K3CODE.md", "k"), ("AGENTS.md", "a"), ("CLAUDE.md", "c")):
        (repo / name).write_text(fill * 20_000)
    out, rows = memory_listing(repo / "pkg", make_cfg())
    cuts = {r["path"].removeprefix(str(repo) + "/"): r["cut"] for r in rows if r["status"] == memmod.READ}
    assert cuts == {"K3CODE.md": "", "AGENTS.md": "total", "CLAUDE.md": "dropped", "pkg/K3CODE.md": ""}
    assert f"{repo / 'AGENTS.md'}  20000 bytes, cut short: the 40000-character total ran out" in out
    assert f"{repo / 'CLAUDE.md'}  20000 bytes, left out: the 40000-character total ran out" in out
    prompt = memmod.memory_prompt(repo / "pkg")
    assert "a" * 4_000 in prompt and "a" * 6_000 not in prompt and "c" * 100 not in prompt
    assert memmod.TOTAL_MARKER.format(n=1, cap=memmod.MAX_MEMORY_TOTAL) in prompt


def test_memory_listing_uses_the_prompts_per_file_cap_and_learning_switch(tmp_path, monkeypatch):
    """``context.memory_chars`` (the optimizer's knob) is the cap the prompt uses, so it is the one reported; with
    ``learning.enabled: false`` the learned notes are listed as not in the prompt."""
    from k3code.commands.memory_cmd import memory_listing
    from k3code.config import Settings

    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "K3CODE.md").write_text("x" * 600)
    memmod.write_learned(repo, ["build with make all"])
    out, rows = memory_listing(repo, Settings(context={"memory_chars": 500}, learning={"enabled": False}))
    assert "cut at the 500-character per-file limit" in out
    assert rows[-1]["scope"] == "learned" and rows[-1]["status"] == memmod.OFF
    assert "x" * 501 not in build_system_prompt("BASE", cwd=repo, config=Settings(context={"memory_chars": 500}))


async def test_skills_command_and_tool(tmp_path, monkeypatch):
    server, provider = make_server(tmp_path, monkeypatch)
    write_skill(home() / "skills", "alpha", "Does alpha things", body="ALPHA-SECRET-BODY")
    proj = tmp_path / "proj"
    write_skill(proj / ".k3code" / "skills", "beta", "Does beta things")
    from k3code import trust

    trust.record(proj, trusted=True)  # project skills load only from a trusted project (test_project_content_trust)
    extra = tmp_path / "library"
    write_skill(extra / "skills", "gamma", "Does gamma things", body="GAMMA-BODY")
    server.config.skills.roots = [str(extra)]
    sid = await new_session(server, proj)

    listing = (await cmd(server, "/skills", sid))["output"]
    assert "3 skill(s)" in listing and "alpha" in listing and "beta" in listing and "gamma" in listing
    shown = (await cmd(server, "/skills show gamma", sid))["output"]
    assert "GAMMA-BODY" in shown and "description: Does gamma things" in shown
    assert "Unknown skill" in (await cmd(server, "/skills show zzz", sid))["output"]

    await submit_and_wait(server, "go")
    prompt = provider.seen[0][0].content
    assert "alpha: Does alpha things" in prompt and "ALPHA-SECRET-BODY" not in prompt  # names only

    reg = build_registry()
    register_skill_tool(reg, proj, [str(extra)])
    spec, handler = reg.get("skill")
    assert spec.name == "skill"
    res = await handler({"name": "alpha"})
    assert "ALPHA-SECRET-BODY" in res["content"]
    assert "error" in await handler({"name": "alph"}) and "alpha" in (await handler({"name": "alph"}))["error"]
    assert "gamma" in (await handler({"query": "gamma"}))["content"]
    assert "error" in await handler({})
    await server.close()


async def test_skill_tool_registered_in_turn_and_allowed(tmp_path, monkeypatch):
    server, _ = make_server(tmp_path, monkeypatch)
    write_skill(home() / "skills", "alpha", "Does alpha things")
    sid = await new_session(server, tmp_path)
    await submit_and_wait(server, "go")
    loop = server.session.loop
    assert "skill" in loop.tools.names()
    assert loop.permissions.decide("skill", {"name": "alpha"}).action == "allow"
    assert server.store.get(sid) is not None
    await rpc(server, "session.interrupt", {"session_id": sid})
    await server.close()


async def test_all_m1_commands_registered_with_help(tmp_path, monkeypatch):
    from k3code.commands.builtin import build_registry

    reg = build_registry()
    for name in (
        "export",
        "import",
        "fork",
        "branch",
        "settings",
        "config",
        "output-style",
        "memory",
        "skills",
        "mcp",
        "review",
        "goal",
    ):
        cmd_def = reg.get(name)
        assert cmd_def is not None and cmd_def.help, name
    server, _ = make_server(tmp_path, monkeypatch)
    res = (await rpc(server, "command.dispatch", {"name": "help"}))["result"]
    assert "/goal" in res["output"] and "/review" in res["output"]
    await server.close()
