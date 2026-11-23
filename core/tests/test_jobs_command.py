"""/jobs and /pause: control the k3jobs 24/7 engine through its --json CLI (fake runner, no real k3jobs or ssh)."""

from __future__ import annotations

import asyncio
import json
import stat
from types import SimpleNamespace

import pytest

from k3code.commands import jobs as jobs_mod
from k3code.commands.builtin import build_registry
from k3code.config import Settings


def make_ctx(**jobs_cfg):
    return SimpleNamespace(config=Settings.model_validate({"jobs": jobs_cfg}))


JOBS = [
    {"name": "alpha", "status": "idle", "paused": False, "liveness": "ok", "ok_rate_24h": 0.5, "next_run": "10:00"},
    {"name": "beta", "status": "failing", "paused": False, "liveness": "failing", "ok_rate_24h": 0.0, "next_run": None},
    {"name": "gamma", "status": "idle", "paused": True, "liveness": "ok", "ok_rate_24h": None, "next_run": None},
]


class FakeK3jobs:
    """Stands in for run_k3jobs: records every call, answers from a table of (code, doc, stderr)."""

    def __init__(self, answers=None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    async def __call__(self, cfg, args):
        self.calls.append(list(args))
        code, doc, err = self.answers.get(tuple(args), self.answers.get(args[0], (0, {"ok": True}, "")))
        return code, json.dumps(doc) if doc is not None else "", err


@pytest.fixture
def fake(monkeypatch):
    f = FakeK3jobs({"list": (0, {"ok": True, "jobs": JOBS}, "")})
    monkeypatch.setattr(jobs_mod, "run_k3jobs", f)
    return f


async def run(name, arg="", **jobs_cfg):
    out = await build_registry().dispatch(make_ctx(**jobs_cfg), name, arg, None)
    return out["output"]


def test_registry_has_jobs_and_pause_and_keeps_resume_for_sessions():
    reg = build_registry()
    assert isinstance(reg.get("jobs"), jobs_mod.JobsCommand)
    assert isinstance(reg.get("pause"), jobs_mod.PauseCommand)
    assert not isinstance(reg.get("resume"), (jobs_mod.JobsCommand, jobs_mod.PauseCommand))


async def test_jobs_lists_a_table(fake):
    out = await run("jobs")
    assert fake.calls == [["list"]]
    lines = out.splitlines()
    assert lines[0].split() == ["name", "state", "liveness", "ok", "24h", "next", "run"]
    assert "alpha" in out and "50%" in out and "10:00" in out
    assert "gamma" in out and "paused" in out  # a paused job shows as paused, not as its last status


async def test_status_summarises_job_runs_and_log_tail(fake):
    fake.answers["status"] = (
        0,
        {
            "ok": True,
            "job": {**JOBS[0], "model": "auto/everything", "interval": "30m", "ok_24h": 2, "runs_24h": 4},
            "timer": {"enabled": True, "next_run": None},
            "runs": [{"ts": f"2026112{i}", "status": "ok", "model": "m", "error": None} for i in range(7)],
            "log_tail": "\n".join(f"line {i}" for i in range(30)),
        },
        "",
    )
    out = await run("jobs", "status alpha")
    assert fake.calls == [["status", "alpha"]]
    assert "alpha: idle" in out and "auto/everything" in out and "2/4" in out and "timer on" in out
    assert out.count("  2026112") == 5  # only the last 5 runs
    assert "line 29" in out and "line 0" not in out  # the tail, not the whole log


async def test_pause_one_job(fake):
    out = await run("pause", "alpha")
    assert fake.calls == [["pause", "alpha"]]
    assert out.startswith("Paused alpha.") and "/jobs resume alpha" in out


async def test_jobs_pause_subcommand_matches_pause(fake):
    assert await run("jobs", "pause alpha") == await run("pause", "alpha")


async def test_pause_without_argument_shows_usage_and_running_jobs(fake):
    out = await run("pause")
    assert "Usage: /pause <job|all>" in out
    assert "Running jobs: alpha, beta" in out  # gamma is already paused
    assert fake.calls == [["list"]]  # nothing was paused


async def test_pause_all_skips_paused_and_continues_after_a_failure(fake):
    fake.answers[("pause", "alpha")] = (5, {"ok": False, "error": "refused", "message": "KILL file present"}, "")
    out = await run("pause", "all")
    assert fake.calls == [["list"], ["pause", "alpha"], ["pause", "beta"]]  # gamma skipped, beta still tried
    assert out.splitlines()[0] == "Paused 1 of 2 running jobs."
    assert "alpha: refused by k3jobs" in out and "KILL file present" in out
    assert "beta: paused" in out


async def test_pause_all_when_everything_is_paused(fake):
    fake.answers["list"] = (0, {"ok": True, "jobs": [{**JOBS[2]}]}, "")
    assert "Nothing to pause" in await run("pause", "all")
    assert fake.calls == [["list"]]


async def test_resume_calls_resume_and_reports_probe_failure(fake):
    assert (await run("jobs", "resume alpha")).startswith("Resumed alpha.")
    fake.answers[("resume", "beta")] = (4, {"ok": False, "error": "probe_failed", "message": "model 503"}, "")
    out = await run("jobs", "resume beta")
    assert "model probe failed" in out and "model 503" in out and not out.startswith("Resumed")


@pytest.mark.parametrize("bad", ["Alpha", "a;rm -rf /", "../x", "a", "x" * 40, "-rf", "abc\n", "ab cd"])
async def test_invalid_job_names_never_reach_k3jobs(fake, bad):
    # split_args may split a name containing spaces, so drive the helpers directly as well
    cfg = make_ctx().config.jobs
    for fn in (jobs_mod.do_pause, jobs_mod.do_resume, jobs_mod.do_status):
        assert "not a valid job name" in await fn(cfg, bad)
    assert fake.calls == []


async def test_bad_usage_prints_usage(fake):
    assert "Usage:" in await run("jobs", "frobnicate")
    assert "Usage:" in await run("jobs", "pause")
    assert "Usage:" in await run("pause", "a b")
    assert fake.calls == []


@pytest.mark.parametrize(
    ("code", "fragment"),
    [(3, "unknown job"), (5, "nothing changed"), (2, "rejected the arguments"), (1, "exited 1"), (127, "not found")],
)
async def test_exit_codes_become_plain_messages(fake, code, fragment):
    fake.answers[("pause", "alpha")] = (code, {"ok": False, "message": "m"}, "")
    assert fragment in await run("pause", "alpha")


async def test_ssh_failure_and_timeout_name_the_cause(fake):
    fake.answers[("pause", "alpha")] = (255, None, "ssh: connect to host k3server port 22: Connection refused")
    out = await run("pause", "alpha", ssh_host="k3server")
    assert "could not reach k3server over ssh" in out
    fake.answers[("pause", "alpha")] = (124, None, "")  # no stderr: the message must come from the 124 mapping itself
    assert "timed out" in await run("pause", "alpha")


async def test_unparseable_success_output_is_reported_not_hidden(monkeypatch):
    async def junk(cfg, args):
        return 0, "not json", ""

    monkeypatch.setattr(jobs_mod, "run_k3jobs", junk)
    assert "not JSON" in await run("jobs")


@pytest.mark.parametrize("output", ["not json", '{"ok": false}', "[]"])
async def test_pause_and_resume_need_an_ok_json_answer(monkeypatch, output):
    async def junk(cfg, args):
        return 0, output, ""

    monkeypatch.setattr(jobs_mod, "run_k3jobs", junk)
    for name, arg in (("pause", "alpha"), ("jobs", "pause alpha"), ("jobs", "resume alpha")):
        out = await run(name, arg)
        assert not out.startswith(("Paused", "Resumed")) and "state unknown" in out


async def test_pause_all_does_not_count_an_unacknowledged_pause(fake):
    fake.answers[("pause", "alpha")] = (0, {"ok": False}, "")
    out = await run("pause", "all")
    assert out.splitlines()[0] == "Paused 1 of 2 running jobs."
    assert "alpha: k3jobs exited 0" in out and "beta: paused" in out


async def test_failed_calls_set_error_and_successes_do_not(fake):
    reg, ctx = build_registry(), make_ctx()
    ok = await reg.dispatch(ctx, "pause", "alpha", None)
    assert "error" not in ok
    fake.answers[("pause", "alpha")] = (5, {"ok": False}, "")
    fake.answers[("resume", "alpha")] = (255, None, "")
    for name, arg in (("pause", "alpha"), ("jobs", "resume alpha"), ("pause", "Bad_Name"), ("jobs", "status Bad_Name")):
        assert (await reg.dispatch(ctx, name, arg, None)).get("error") == "command_failed", (name, arg)
    fake.answers[("pause", "beta")] = (5, {"ok": False}, "")  # a partial /pause all is a failure too
    assert (await reg.dispatch(ctx, "pause", "all", None)).get("error") == "command_failed"
    fake.answers["list"] = (0, {"ok": True, "jobs": [JOBS[0]]}, "")
    fake.answers[("pause", "alpha")] = (0, {"ok": True}, "")
    assert "error" not in await reg.dispatch(ctx, "pause", "all", None)


async def test_headless_pause_all_fails_when_the_host_is_unreachable(fake):
    from k3code.cli import _run_headless_slash

    fake.answers["list"] = (255, None, "ssh: connect to host k3server port 22: Connection refused")
    cmd = jobs_mod.PauseCommand()
    result = await _run_headless_slash(cmd, "pause", "all", make_ctx(ssh_host="k3server").config)
    assert result["error"] == "command_failed" and "could not reach" in result["message"]


def test_local_argv_expands_tilde_and_appends_json(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    argv, env = jobs_mod.build_call(make_ctx().config.jobs, ["pause", "alpha"])
    assert argv == ["/home/someone/k3jobs/bin/k3jobs", "pause", "alpha", "--json"] and env is None
    _, env = jobs_mod.build_call(make_ctx(root="/data/k3jobs").config.jobs, ["list"])
    assert env is not None and env["K3JOBS_ROOT"] == "/data/k3jobs"


def test_ssh_argv_is_one_quoted_remote_command_with_home_expansion():
    cfg = make_ctx(ssh_host="k3server", root="/mnt/my root").config.jobs
    argv, env = jobs_mod.build_call(cfg, ["pause", "alpha"])
    assert env is None
    assert argv[:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--"] and argv[6] == "k3server"
    assert len(argv) == 8  # host + exactly one remote command string
    assert argv[7] == "env K3JOBS_ROOT='/mnt/my root' \"$HOME\"/k3jobs/bin/k3jobs pause alpha --json"


async def test_ssh_host_that_looks_like_an_option_is_refused(monkeypatch):
    spawned = []

    async def boom(*a, **k):  # must never run
        spawned.append(a)

    monkeypatch.setattr(jobs_mod.asyncio, "create_subprocess_exec", boom)
    out = await run("jobs", ssh_host="-oProxyCommand=evil")
    assert "invalid jobs config" in out and "looks like an ssh option" in out
    assert spawned == []


async def test_real_subprocess_round_trip_and_timeout(tmp_path):
    script = tmp_path / "fake-k3jobs"
    script.write_text(
        '#!/bin/sh\nif [ "$1" = slow ]; then sleep 5; fi\n'
        'echo \'{"ok": true, "jobs": [{"name": "alpha", "status": "idle", "paused": false}]}\'\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    out = await run("jobs", bin=str(script))
    assert "alpha" in out and "idle" in out
    slow = await jobs_mod.run_k3jobs(make_ctx(bin=str(script), timeout_s=0.3).config.jobs, ["slow"])
    assert slow[0] == 124 and "timed out" in slow[2]
    missing = await jobs_mod.run_k3jobs(make_ctx(bin=str(tmp_path / "nope")).config.jobs, ["list"])
    assert missing[0] == 127


async def test_pause_all_never_reports_all_clear_on_an_unusable_list(monkeypatch):
    calls = []

    async def junk(cfg, args):
        calls.append(list(args))
        return 0, "Usage: not the k3jobs you wanted", ""

    monkeypatch.setattr(jobs_mod, "run_k3jobs", junk)
    for name, arg in (("pause", "all"), ("jobs", "pause all"), ("pause", "")):
        out = await run(name, arg)
        assert "not JSON with a jobs list" in out
        assert "Nothing to pause" not in out and "Running jobs: none" not in out
    assert all(c == ["list"] for c in calls)  # nothing was paused


@pytest.mark.parametrize(
    "jobs",
    [
        ["alpha", "beta"],
        [{"id": "alpha", "paused": False}],
        [{"name": "alpha", "paused": "false"}],
        [{"name": "alpha"}],
    ],
)
async def test_unreadable_job_entries_are_a_bad_list_not_an_all_clear(fake, jobs):
    fake.answers["list"] = (0, {"ok": True, "jobs": jobs}, "")
    for name, arg in (("pause", "all"), ("pause", ""), ("jobs", "")):
        out = await run(name, arg)
        assert "not JSON with a jobs list" in out
        assert "Nothing to pause" not in out and "Running jobs: none" not in out
    assert all(c == ["list"] for c in fake.calls)


async def test_pause_all_skips_unexpected_names(fake):
    fake.answers["list"] = (0, {"ok": True, "jobs": [{"name": "Bad_Name", "paused": False}, JOBS[0]]}, "")
    out = await run("pause", "all")
    assert fake.calls == [["list"], ["pause", "alpha"]]
    assert "Bad_Name: skipped" in out and out.splitlines()[0] == "Paused 1 of 2 running jobs."


def test_root_tilde_expands_locally_and_on_the_remote(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    _, env = jobs_mod.build_call(make_ctx(root="~/data/k3jobs").config.jobs, ["list"])
    assert env is not None and env["K3JOBS_ROOT"] == "/home/someone/data/k3jobs"
    argv, _ = jobs_mod.build_call(make_ctx(ssh_host="h", root="~/data/k3jobs").config.jobs, ["list"])
    assert argv[7].startswith('env K3JOBS_ROOT="$HOME"/data/k3jobs ')


async def test_timeout_survives_a_process_that_already_exited(monkeypatch):
    class Proc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            raise ProcessLookupError

        async def wait(self):
            return 0

    async def spawn(*a, **k):
        return Proc()

    monkeypatch.setattr(jobs_mod.asyncio, "create_subprocess_exec", spawn)
    code, _, err = await jobs_mod.run_k3jobs(make_ctx(timeout_s=0.1).config.jobs, ["list"])
    assert code == 124 and "timed out" in err


def test_commands_run_headless_without_a_server():
    for cmd in (jobs_mod.JobsCommand(), jobs_mod.PauseCommand()):
        assert cmd.headless is True and cmd.needs_server is False
