"""M2-ops: doctor, stats, debug dump, sandbox, /model chain."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from k3code import chain_config, debugdump, doctor
from k3code.config import ProviderEntry, Settings
from k3code.permissions import PermissionMode
from k3code.reliability import sandbox
from k3code.router.classifier import FailoverReason
from k3code.tools import tool_bash
from k3code.usage import UsageDB
from test_gateway import make_server


def _config(*providers: ProviderEntry) -> Settings:
    return Settings(providers=list(providers))


def _entry(name="p1", url="http://127.0.0.1:9/v1", env="K3_TEST_KEY") -> ProviderEntry:
    return ProviderEntry(name=name, kind="openai", base_url=url, api_key_env=env, models={"default": "m1"})


# ── doctor ──


async def test_doctor_json_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("K3_TEST_KEY", "sk-should-never-appear-123")
    checks = await doctor.run_checks(_config(_entry()), probe=False, home=tmp_path)
    data = json.loads(doctor.to_json(checks))
    assert set(data) == {"summary", "checks"} and set(data["summary"]) == {"ok", "warn", "fail"}
    names = {c["name"] for c in data["checks"]}
    for want in (
        "provider:p1",
        "api-keys",
        "netwatch",
        "disk",
        "psi",
        "daemon",
        "systemd-unit",
        "tui",
        "k3code-home",
        "journal",
        "vendor",
        "sandbox",
        "hermes-isolation",
    ):
        assert want in names, want
    assert all(c["status"] in ("ok", "warn", "fail") and {"detail", "fix"} <= set(c) for c in data["checks"])
    assert sum(data["summary"].values()) == len(data["checks"])
    assert "sk-should-never-appear-123" not in doctor.to_json(checks)
    assert "omniroute-bypass" not in names  # only shown when the chain has an OmniRoute entry


async def test_doctor_fails_a_rejected_key(tmp_path, monkeypatch):
    from k3code.setup import probe as setup_probe

    monkeypatch.setenv("K3_TEST_KEY", "sk-bad")
    monkeypatch.setattr(setup_probe, "list_models", lambda entry, key, timeout=8.0: (False, 12.0, [], "HTTP 401"))
    checks = await doctor.check_providers(_config(_entry()), probe=True)
    prov = next(c for c in checks if c.name == "provider:p1")
    assert prov.status == doctor.FAIL and "rejected" in prov.detail and "k3code onboard" in prov.fix


async def test_doctor_omniroute_bypass_and_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("K3_TEST_KEY", raising=False)
    omni = _entry("omniroute", "http://localhost:20128/v1")
    checks = {c.name: c for c in await doctor.run_checks(_config(omni), probe=False, home=tmp_path)}
    assert checks["omniroute-bypass"].status == "warn"
    assert checks["api-keys"].status == "fail" and "K3_TEST_KEY" in checks["api-keys"].detail
    direct = {
        c.name: c
        for c in await doctor.run_checks(
            _config(omni, _entry("direct", "https://api.anthropic.com/v1")), probe=False, home=tmp_path
        )
    }
    assert direct["omniroute-bypass"].status == "ok"


async def test_doctor_provider_probe_unreachable(tmp_path):
    checks = {c.name: c for c in await doctor.run_checks(_config(_entry()), probe=True, home=tmp_path)}
    assert checks["provider:p1"].status == "fail" and checks["provider:p1"].fix


def test_doctor_journal_unresolved_intent(tmp_path):
    from k3code.reliability.journal import ToolJournal

    j = ToolJournal(tmp_path, "s1")
    j.record_intent("c1", "bash", {"command": "echo"}, side_effect=True)
    j.close()
    assert doctor.check_journal(tmp_path).status == "warn"
    assert doctor.check_journal(tmp_path / "empty").status == "ok"


def test_doctor_isolation_flags_hermes_env(monkeypatch):
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    assert doctor.check_isolation().status == "ok"
    monkeypatch.setenv("HERMES_HOME", "/x")
    assert doctor.check_isolation().status == "warn"


# ── stats ──


def test_stats_aggregation(tmp_path):
    db = UsageDB(tmp_path / "usage.db")
    day1, day2 = 1_700_000_000.0, 1_700_000_000.0 + 86400 * 2
    db.record("call", session="a", provider="p", model="m", tokens_in=10, tokens_out=5, ts=day1)
    db.record("call", session="a", provider="p", model="m", tokens_in=20, tokens_out=1, cost_usd=0.5, ts=day1 + 1)
    db.record("call", session="b", provider="q", model="n", tokens_in=1, tokens_out=1, ts=day2)
    db.record("failover", session="a", ts=day1 + 2)
    db.record("retry", session="a", ts=day1 + 3)
    db.record("pause", session="a", seconds=12.5, ts=day1 + 4)
    db.record("tool", session="a", detail="bash", ts=day1 + 5)
    db.record("tool", session="a", detail="read", ts=day1 + 6)
    db.record("approval", session="a", ts=day1 + 7)
    by_session = {g["key"]: g for g in db.aggregate("session")}
    a = by_session["a"]
    assert (a["tokens_in"], a["tokens_out"], a["calls"], a["cost_usd"]) == (30, 6, 2, 0.5)
    assert (a["failovers"], a["retries"], a["pauses"], a["paused_seconds"]) == (1, 1, 1, 12.5)
    assert (a["tool_calls"], a["approvals"]) == (2, 1)
    assert a["by_model"] == {"p/m": 2}
    assert by_session["b"]["cost_usd"] is None  # unknown stays unknown
    by_day = db.aggregate("day")
    assert len(by_day) == 2 and sum(g["calls"] for g in by_day) == 3


# ── debug dump ──


async def test_debug_dump_contains_no_secrets(tmp_path, monkeypatch):
    secret = "sk-test-redact-me-9f8e7d"
    monkeypatch.setenv("K3_TEST_KEY", secret)
    monkeypatch.setenv("SOME_OTHER_TOKEN", "tok-another-secret-value")
    cfg = _config(_entry())
    cfg.providers[0].api_key = secret
    server = make_server(config=cfg)
    server.emit("error", {"message": f"leaked {secret} and Bearer abcdefghijklmnop and tok-another-secret-value"})
    path = await debugdump.write_bundle(server, 50, home=tmp_path, probe=False)
    assert path.parent == tmp_path / "debug" and path.suffixes[-2:] == [".tar", ".gz"]
    with tarfile.open(path) as tar:
        names = set(tar.getnames())
        assert {"events.json", "config.json", "versions.json", "doctor.json"} <= names
        blob = b"".join(tar.extractfile(n).read() for n in names)
    for s in (secret, "tok-another-secret-value", "abcdefghijklmnop"):
        assert s.encode() not in blob
    assert b"[REDACTED]" in blob


async def test_debug_command_toggles_and_dumps(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    server = make_server(config=_config(_entry()))
    assert (await server.dispatch_command("debug", "", None))["message"].endswith("ON.")
    assert server.debug is True
    out = (await server.dispatch_command("debug", "dump 10", None))["message"]
    assert Path(out.split(": ", 1)[1]).is_file()
    assert (await server.dispatch_command("debug", "", None))["message"].endswith("OFF.")


# ── sandbox ──


def test_unattended_sessions_are_sandboxed_in_any_mode():
    assert sandbox.should_sandbox(PermissionMode.DEFAULT, False, unattended=True)  # e.g. goal continuation, sub-agent
    assert not sandbox.should_sandbox(PermissionMode.DEFAULT, False, unattended=False)


def test_bwrap_argv_policy(tmp_path):
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)
    (home / ".local/share/uv").mkdir(parents=True)
    proj, extra = tmp_path / "proj", tmp_path / "extra"
    proj.mkdir()
    extra.mkdir()
    argv = sandbox.build_argv(proj, [extra], home=home, bwrap="/usr/bin/bwrap")
    joined = " ".join(argv)
    assert argv[0] == "/usr/bin/bwrap" and "--die-with-parent" in argv
    assert "--unshare-net" not in argv  # network stays on
    assert "--ro-bind /usr /usr" in joined and "--ro-bind /etc /etc" in joined
    assert f"--tmpfs {home}" in joined  # $HOME hidden …
    assert f"--bind {home}/.cache {home}/.cache" in joined  # … except these
    assert f"--ro-bind {home}/.local/share/uv {home}/.local/share/uv" in joined
    assert f"--bind {proj.resolve()} {proj.resolve()}" in joined and f"--bind {extra.resolve()}" in joined
    assert argv.index("--tmpfs") < argv.index(f"{proj.resolve()}")  # project is bound after the home tmpfs


def test_bwrap_never_rebinds_home_and_masks_secrets(tmp_path, monkeypatch):
    """cwd = $HOME (or /) was bound read-write after the home tmpfs: unattended bash saw ~/.config/k3code/env and
    ~/.ssh. Such a root is refused (fail closed); a project inside $HOME is bound, and the secrets are masked after
    every bind."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = tmp_path / "home"
    (home / ".config/k3code").mkdir(parents=True)
    (home / ".ssh").mkdir()
    for cwd in (home, "/"):
        with pytest.raises(sandbox.SandboxRefused):
            sandbox.build_argv(cwd, [home / ".config"], home=home, bwrap="/usr/bin/bwrap")
    argv = sandbox.build_argv(home / "proj", [home / ".config"], home=home, bwrap="/usr/bin/bwrap")
    joined = " ".join(argv)
    assert f"--bind {home} " not in joined
    assert f"--bind {home}/.config {home}/.config" in joined  # a dir inside $HOME is still bound …
    last_bind = max(i for i, a in enumerate(argv) if a == "--bind")
    for secret in (home / ".config/k3code", home / ".ssh"):  # … but the secrets are masked after every bind
        assert argv.index(str(secret)) > last_bind and argv[argv.index(str(secret)) - 1] == "--tmpfs"
    assert sandbox.exposes_home(home, home) and sandbox.exposes_home("/", home)
    assert not sandbox.exposes_home(home / "proj", home)


def test_sandbox_policy_by_mode():
    assert sandbox.should_sandbox(PermissionMode.AUTO, False) and sandbox.should_sandbox("yolo", False)
    assert sandbox.should_sandbox(PermissionMode.DEFAULT, True)  # background/cron/loop
    assert not sandbox.should_sandbox(PermissionMode.DEFAULT, False)


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_sandboxed_bash_cannot_write_home(tmp_path):
    target = Path.home() / "sandbox-escape-test"
    target.unlink(missing_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    argv = sandbox.build_argv(proj)
    cmd = f"echo pwned > {target}; echo ok > inside.txt; ls {Path.home()} | head -50"
    res = await tool_bash({"command": cmd}, cwd=proj, sandbox=argv)
    assert not target.exists(), "sandboxed bash wrote into the real $HOME"
    assert (proj / "inside.txt").read_text().strip() == "ok"  # the project dir is writable
    assert res["exit_code"] == 0
    unsandboxed = await tool_bash({"command": "true"}, cwd=proj)
    assert unsandboxed["exit_code"] == 0


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_agent_loop_sandboxes_only_unattended(tmp_path):
    from k3code.agent.loop import AgentLoop

    class _R:  # router stub: the loop only needs it for attach_router
        chain: list = []

    loop = AgentLoop(_R(), system_prompt="x", cwd=tmp_path, permission_mode="ask")  # type: ignore[arg-type]
    assert loop._sandbox_argv() is None
    loop.permission_mode = PermissionMode.YOLO
    assert loop._sandbox_argv() is not None
    loop.permission_mode = PermissionMode.DEFAULT
    loop.background = True
    assert loop._sandbox_argv() is not None


def test_interactive_loop_runs_unsandboxed_with_a_warning_when_bwrap_missing(tmp_path, monkeypatch, caplog):
    from k3code.agent.loop import AgentLoop

    class _R:
        chain: list = []

    monkeypatch.setattr(sandbox, "usable", lambda: False)
    loop = AgentLoop(_R(), system_prompt="x", cwd=tmp_path, permission_mode="yolo")  # type: ignore[arg-type]
    assert loop._sandbox_argv() is None  # a user is watching: runs unsandboxed, /doctor warns
    assert "bwrap unavailable" in caplog.text


async def test_unattended_bash_is_refused_when_bwrap_is_unusable(tmp_path, monkeypatch):
    """Background, goal and sub-agent bash fail closed: the model gets an error and no process is started."""
    import asyncio

    from k3code.agent.loop import AgentLoop
    from k3code.providers.types import ToolCall

    class _R:
        chain: list = []

    monkeypatch.setattr(sandbox, "usable", lambda: False)
    spawned: list[tuple] = []

    async def refuse_spawn(*argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("an unattended command must not be spawned without bwrap")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse_spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_spawn)
    for kwargs in ({"background": True}, {"unattended": True}):
        loop = AgentLoop(_R(), system_prompt="x", cwd=tmp_path, permission_mode="yolo", **kwargs)  # type: ignore[arg-type]
        result = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": "touch made.txt"}))
        assert "bash refused" in result["error"] and "bubblewrap is unusable" in result["error"]
    assert spawned == [] and not (tmp_path / "made.txt").exists()


def test_doctor_warns_without_bwrap(monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: None)
    assert doctor.check_sandbox().status == "warn"


# ── /model chain ──


@pytest.fixture
def chain_home(tmp_path, monkeypatch):
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path))
    cfg = {
        "providers": [
            {
                "name": "omni",
                "kind": "openai",
                "base_url": "http://o/v1",
                "api_key_env": "O_KEY",
                "models": {"default": ["a", "b"]},
            },
            {
                "name": "direct",
                "kind": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key_env": "A_KEY",
                "models": {"default": "c"},
            },
        ]
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    return tmp_path


def _names(home: Path) -> list[str]:
    return [p["name"] for p in yaml.safe_load((home / "config.yaml").read_text())["providers"]]


def test_chain_move_remove_add_with_backup(chain_home):
    backup = chain_config.edit_config("move", ["direct", "1"])
    assert _names(chain_home) == ["direct", "omni"] and Path(backup).is_file()
    assert yaml.safe_load(Path(backup).read_text())["providers"][0]["name"] == "omni"  # backup = pre-edit
    chain_config.edit_config("add", ["omni", "z"])
    models = yaml.safe_load((chain_home / "config.yaml").read_text())["providers"][1]["models"]["default"]
    assert models == ["a", "b", "z"]
    chain_config.edit_config("remove", ["omni", "a"])
    chain_config.edit_config("remove", ["direct"])
    assert _names(chain_home) == ["omni"]
    chain_config.edit_config("add", ["new", "m", "kind=openai", "base_url=http://n/v1", "key_env=N_KEY"])
    assert _names(chain_home) == ["omni", "new"]


def test_chain_invalid_edit_leaves_config_untouched(chain_home):
    before = (chain_home / "config.yaml").read_text()
    for op, args in (
        ("remove", ["nope"]),
        ("add", ["ghost", "m"]),
        ("move", ["omni", "x"]),
        ("bogus", []),
        ("add", ["x", "m", "kind=weird", "base_url=u", "key_env=K"]),
    ):
        with pytest.raises(chain_config.ChainEditError):
            chain_config.edit_config(op, args)
    assert (chain_home / "config.yaml").read_text() == before
    assert not list(chain_home.glob("config.yaml.bak.*"))  # validation runs before any backup


async def test_model_chain_command_shows_cooldown(chain_home):
    cfg = Settings(
        providers=[
            ProviderEntry(**p, api_key="")
            for p in yaml.safe_load((chain_home / "config.yaml").read_text())["providers"]
        ]
    )
    server = make_server(config=cfg)
    server.cooldowns.arm(FailoverReason.rate_limit, provider="omni", model="a", base_url="http://o/v1", retry_after=90)
    text = (await server.dispatch_command("model", "chain", None))["message"]
    assert "1. omni/a [cooldown]" in text and "2. omni/b" in text and "3. direct/c" in text
    out = (await server.dispatch_command("model", "chain move direct 1", None))["message"]
    assert out.startswith("Chain updated") and "1. direct/c" in out


def test_sandbox_argv_drops_the_daemon_environment(tmp_path):
    argv = sandbox.build_argv(tmp_path, bwrap="/usr/bin/bwrap")
    assert "--clearenv" in argv and "--unshare-ipc" in argv
    assert "--setenv" in argv and argv[argv.index("--setenv") + 1] in sandbox.ENV_ALLOW
    allowed = {*sandbox.ENV_ALLOW, *sandbox.PROXY_ENV}
    assert all(name in allowed for name in (argv[i + 1] for i, a in enumerate(argv) if a == "--setenv"))


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_sandboxed_bash_does_not_see_provider_api_keys(tmp_path, monkeypatch):
    """The daemon's environment holds the provider keys; a prompt-injected `echo $KEY` must find nothing."""
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    argv = sandbox.build_argv(tmp_path)
    res = await tool_bash({"command": "env; echo path=$PATH"}, cwd=tmp_path, sandbox=argv)
    assert "sk-secret-value" not in res["stdout"] and "sk-ant-secret" not in res["stdout"]
    assert "OMNIROUTE_API_KEY" not in res["stdout"] and "path=/" in res["stdout"]  # PATH survives


def test_a_failed_bwrap_probe_is_retried_not_latched(monkeypatch):
    calls = []

    def fake_probe() -> bool:
        calls.append(1)
        return len(calls) > 1  # first probe fails (a transient timeout), the second succeeds

    sandbox.reset_probe()
    monkeypatch.setattr(sandbox, "_probe_bwrap", fake_probe)
    clock = [1000.0]
    monkeypatch.setattr(sandbox.time, "monotonic", lambda: clock[0])
    assert sandbox.usable() is False
    assert sandbox.usable() is False and len(calls) == 1  # inside the back-off window: no new probe
    clock[0] += sandbox.REPROBE_AFTER_S + 1
    assert sandbox.usable() is True and len(calls) == 2  # re-probed, and now positive
    clock[0] += 10_000
    assert sandbox.usable() is True and len(calls) == 2  # a positive result is cached
    sandbox.reset_probe()


# ── one gate for unattended children (P2-1) ──

_SECRET_KEYS = {"K3CODE_API_KEY": "sk-k3-secret-111", "OMNIROUTE_API_KEY": "sk-omni-secret-222"}


async def test_goal_gate_and_fanout_children_never_get_provider_keys(tmp_path, monkeypatch):
    from k3code.autonomy.fanout import run_tests
    from k3code.goals import GoalGate, run_gate

    for name, value in _SECRET_KEYS.items():
        monkeypatch.setenv(name, value)
    _passed, _code, gate_out = await run_gate(GoalGate(command="env"), cwd=str(tmp_path))
    _ok, fanout_out = await run_tests("env", tmp_path, 30)
    for text in (gate_out, fanout_out):
        assert "sk-k3-secret-111" not in text and "sk-omni-secret-222" not in text
        assert "K3CODE_API_KEY" not in text and "OMNIROUTE_API_KEY" not in text


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_unattended_gate_and_fanout_argv_start_with_bwrap(tmp_path, monkeypatch):
    from k3code.autonomy.fanout import run_tests
    from k3code.goals import GoalGate, run_gate

    spawned: list[tuple[str, ...]] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        spawned.append(argv)
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    passed, code, _ = await run_gate(GoalGate(command="true"), cwd=str(tmp_path))
    ok, _ = await run_tests("true", tmp_path, 30)
    assert passed and code == 0 and ok
    assert len(spawned) == 2 and all(argv[0] == sandbox.bwrap_path() for argv in spawned)


def test_unattended_prefix_refuses_when_bwrap_is_unusable(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "usable", lambda: False)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.unattended_prefix(tmp_path)


async def test_goal_gate_fails_closed_without_bwrap(tmp_path, monkeypatch):
    from k3code.goals import GoalGate, run_gate

    monkeypatch.setattr(sandbox, "usable", lambda: False)
    marker = tmp_path / "ran.txt"
    passed, code, text = await run_gate(GoalGate(command=f"touch {marker}"), cwd=str(tmp_path))
    assert passed is False and code == -1 and "unusable" in text
    assert not marker.exists()  # the check never ran unsandboxed


def test_harness_git_disables_hooks_and_fsmonitor():
    argv = sandbox.harness_git_argv("status", "--porcelain")
    assert argv[0] == "git" and argv[-2:] == ["status", "--porcelain"]
    configs = {argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"}
    assert {"core.hooksPath=/dev/null", "core.fsmonitor=false"} <= configs


async def test_harness_git_children_get_hardened_argv_and_scrubbed_env(tmp_path, monkeypatch):
    from k3code.subagents import worktree

    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-omni-secret-222")
    seen: dict[str, object] = {}
    real_exec = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs.get("env") or {}
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    await worktree.git(tmp_path, "--version")
    argv, env = seen["argv"], seen["env"]
    assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv
    assert "OMNIROUTE_API_KEY" not in env and "PATH" in env


def test_mcp_stdio_children_get_the_scrubbed_env(monkeypatch):
    from k3code import mcpclient

    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-omni-secret-222")
    env = mcpclient.stdio_env({"MY_SERVER_SETTING": "x"})
    assert "OMNIROUTE_API_KEY" not in env and env["MY_SERVER_SETTING"] == "x" and "PATH" in env


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_goal_continuation_bash_is_sandboxed_in_a_foreground_session(tmp_path, monkeypatch):
    from k3code.routing.tiers import TaskKind
    from test_autonomy_gateway import make, start

    server = make(tmp_path, monkeypatch, [], mode="default", autonomy={"plan_first": False, "proposals": False})
    (tmp_path / "proj").mkdir()  # the k3code home lives under tmp_path: a project must not contain it
    await start(server, tmp_path / "proj")
    session = server.session
    user_turn = server._build_loop(session, None, None, TaskKind.INTERACTIVE_TURN, None)
    assert user_turn._sandbox_argv() is None  # the user's own turn in default mode stays unsandboxed
    session.goal_continuation = True
    continuation = server._build_loop(session, None, None, TaskKind.INTERACTIVE_TURN, None)
    assert continuation.unattended and continuation._sandbox_argv()[0] == sandbox.bwrap_path()


# ── sandbox roots, git metadata, network, cwd (P2-4) ──


def test_sandbox_refuses_roots_that_expose_home_ssh_or_the_k3code_home(tmp_path, monkeypatch):
    home, k3 = tmp_path / "home", tmp_path / "k3"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("K3CODE_HOME", str(k3))
    for bad in (home, tmp_path, home / ".ssh", k3):  # $HOME, a parent of it, ~/.ssh, the k3code home
        with pytest.raises(sandbox.SandboxRefused):
            sandbox.build_argv(bad, home=home, bwrap="/usr/bin/bwrap")
    assert sandbox.build_argv(home / "project", home=home, bwrap="/usr/bin/bwrap")[0] == "/usr/bin/bwrap"


def test_git_metadata_is_bound_read_only(tmp_path):
    from m1cmd_helpers import git_repo

    repo = git_repo(tmp_path / "repo").resolve()
    joined = " ".join(sandbox.build_argv(repo, home=tmp_path / "home", bwrap="/usr/bin/bwrap"))
    assert f"--bind {repo} {repo}" in joined
    assert f"--ro-bind {repo / '.git'} {repo / '.git'}" in joined


def test_linked_worktree_gitdir_and_common_dir_are_bound_read_only(tmp_path):
    from m1cmd_helpers import git_repo

    repo = git_repo(tmp_path / "repo").resolve()
    wt = repo / ".k3code" / "worktrees" / "child1"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt), "-b", "k3/child1"], cwd=repo, check=True, capture_output=True
    )
    joined = " ".join(sandbox.build_argv(wt, home=tmp_path / "home", bwrap="/usr/bin/bwrap"))
    gitdir = (repo / ".git" / "worktrees" / "child1").resolve()
    assert f"--ro-bind {gitdir} {gitdir}" in joined  # the linked worktree's admin dir lives outside the checkout
    assert f"--ro-bind {repo / '.git'} {repo / '.git'}" in joined  # the common dir holds the objects


def test_unattended_sandbox_has_no_network_unless_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "usable", lambda: True)
    assert "--unshare-net" in sandbox.build_argv(tmp_path, network=False, bwrap="/usr/bin/bwrap")
    assert "--unshare-net" not in sandbox.build_argv(tmp_path, bwrap="/usr/bin/bwrap")
    assert "--unshare-net" in sandbox.unattended_prefix(tmp_path)


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_sandboxed_bash_starts_in_the_requested_cwd(tmp_path):
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    res = await tool_bash({"command": "pwd", "cwd": "sub"}, cwd=proj, sandbox=sandbox.build_argv(proj))
    assert res["stdout"].strip() == str((proj / "sub").resolve())


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_sandboxed_bash_cannot_write_git_hooks(tmp_path):
    from m1cmd_helpers import git_repo

    repo = git_repo(tmp_path / "repo").resolve()
    hook = repo / ".git" / "hooks" / "pre-commit"
    cmd = f"echo 'touch pwned' > {hook}; echo ok > inside.txt"
    res = await tool_bash({"command": cmd}, cwd=repo, sandbox=sandbox.build_argv(repo))
    assert not hook.exists()
    assert (repo / "inside.txt").read_text().strip() == "ok"  # the working tree itself stays writable
    assert "Read-only file system" in res["stderr"]  # the hook write was refused by the sandbox


# ── escape regression suite (P2-5): each case fails on the pre-fix branch ──


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_escape_bash_with_home_as_cwd_cannot_write_into_home(tmp_path, monkeypatch):
    from k3code.agent.loop import AgentLoop
    from k3code.providers.types import ToolCall

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))  # the sandbox hides and binds Path.home()

    class _R:
        chain: list = []

    loop = AgentLoop(_R(), system_prompt="x", cwd=home, permission_mode="yolo")  # type: ignore[arg-type]
    touch = f"touch {home}/escaped.txt"
    result = await loop._execute_tool(ToolCall(id="c1", name="bash", arguments={"command": touch}))
    assert "bash refused" in result.get("error", "")
    assert not (home / "escaped.txt").exists(), "sandboxed bash with cwd $HOME wrote into $HOME"


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_escape_a_hook_planted_from_the_sandbox_never_runs_under_harness_git(tmp_path):
    from k3code.subagents import worktree
    from m1cmd_helpers import git_repo

    repo = git_repo(tmp_path / "repo").resolve()
    marker = tmp_path / "hook-ran.txt"
    hook = repo / ".git" / "hooks" / "pre-commit"
    body = f"#!/bin/sh\ntouch {marker}\n"
    await tool_bash(
        {"command": f"printf '{body}' > {hook}; chmod +x {hook}"}, cwd=repo, sandbox=sandbox.build_argv(repo)
    )
    (repo / "change.txt").write_text("x\n")
    await worktree.git(repo, "add", "-A")
    await worktree.git(repo, "commit", "-qm", "harness commit")  # the harness's own git
    assert not marker.exists(), "a hook written from the sandbox ran under harness git"


async def test_escape_auto_mode_write_outside_the_project_is_denied(tmp_path):
    from k3code.agent.loop import AgentLoop
    from k3code.providers.types import ToolCall

    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "outside.txt"

    class _R:
        chain: list = []

    loop = AgentLoop(_R(), system_prompt="x", cwd=project, permission_mode="auto")  # type: ignore[arg-type]
    result = await loop._execute_tool(
        ToolCall(id="c1", name="write", arguments={"path": str(target), "content": "pwned"})
    )
    assert "project roots" in result.get("error", "")
    assert not target.exists(), "auto mode wrote outside the project roots"
