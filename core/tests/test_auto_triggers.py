import asyncio
import json
import subprocess

from auto_helpers import FakeRunner, clock, make_db
from k3code.automation.automations import AutomationError, AutomationManager, render
from k3code.automation.runner import RunResult
from k3code.automation.suggestions import Suggestions
from k3code.automation.triggers import CronTrigger, FileChangeTrigger, GitTrigger, IdleTrigger, glob_to_regex
from k3code.automation.webhook import WebhookServer
from m1cmd_helpers import git_repo


async def until(cond, tries=300, step=0.02):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition never became true")


def collector():
    events: list[dict] = []

    async def fire(info):
        events.append(info)

    return events, fire


def test_glob_to_regex():
    g = glob_to_regex("src/**/*.py")
    assert g.match("src/a.py") and g.match("src/x/y/a.py") and not g.match("src/a.txt") and not g.match("lib/a.py")
    assert glob_to_regex("*.md").match("README.md") and not glob_to_regex("*.md").match("docs/README.md")
    assert glob_to_regex("**/*").match("a/b/c")


async def test_file_change_trigger(tmp_path):
    events, fire = collector()
    t = FileChangeTrigger(
        {"type": "file_change", "glob": "**/*.py", "debounce": 0.05}, fire, clock(), cwd=str(tmp_path)
    )
    t.start()
    await asyncio.wait_for(t.ready.wait(), 5)
    await asyncio.sleep(0.3)  # let inotify arm
    (tmp_path / "notes.txt").write_text("ignored")
    (tmp_path / "a.py").write_text("x = 1")
    await until(lambda: events, tries=200)
    assert events[0]["event"] == "file_change" and events[0]["path"] == "a.py"
    assert all(e["path"] != "notes.txt" for e in events)
    await t.stop()


async def test_git_trigger_commit_and_checkout(tmp_path):
    repo = git_repo(tmp_path / "r")
    c = clock()
    events, fire = collector()
    t = GitTrigger({"type": "git", "event": "commit", "branch": "main"}, fire, c, cwd=str(repo), poll_s=10)
    ev2, fire2 = collector()
    t2 = GitTrigger({"type": "git", "event": "checkout"}, fire2, c, cwd=str(repo), poll_s=10)
    t.start()
    t2.start()
    await until(lambda: t.last and t2.last)
    await c.advance(10)
    assert not events  # nothing new yet
    (repo / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "second commit"], cwd=repo, check=True)
    await c.advance(10)
    await until(lambda: events)
    assert events[0]["subject"] == "second commit" and events[0]["event"] == "git_commit"
    assert not ev2  # same branch: no checkout
    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
    await c.advance(10)
    await until(lambda: ev2)
    assert ev2[0]["branch"] == "feature"
    await t.stop()
    await t2.stop()


async def test_idle_trigger_fires_once_and_rearms():
    c = clock()
    last = [c.now()]
    events, fire = collector()
    t = IdleTrigger({"type": "idle", "minutes": 10}, fire, c, activity=lambda: last[0], poll_s=60)
    t.start()
    await c.advance(9 * 60)
    assert not events
    await c.advance(2 * 60)
    assert len(events) == 1
    await c.advance(30 * 60)
    assert len(events) == 1  # once per idle period
    last[0] = c.now()  # user came back
    await c.advance(60)
    await c.advance(11 * 60)
    assert len(events) == 2
    await t.stop()


async def test_cron_trigger_missed_once_and_grace():
    c = clock()
    events, fire = collector()
    t = CronTrigger({"type": "cron", "schedule": "*/5 * * * *"}, fire, c, first_due=c.now() - 3600)
    t.start()
    await c.settle()
    assert len(events) == 1  # missed an hour ago, within grace: fires once
    await t.stop()
    events2, fire2 = collector()
    t2 = CronTrigger({"type": "cron", "schedule": "*/5 * * * *"}, fire2, c, first_due=c.now() - 8 * 3600)
    t2.start()
    await c.settle()
    assert not events2  # outside the 6h grace: skipped
    await c.advance(300)
    assert len(events2) == 1
    await t2.stop()


async def test_cron_trigger_restart_after_a_crash_does_not_refire_the_missed_run():
    """The restart after a crash reused the stale first_due and fired the missed run again at once."""
    c = clock()
    events: list[dict] = []

    async def fire(info):
        events.append(info)
        if len(events) == 1:
            raise RuntimeError("boom")

    t = CronTrigger({"type": "cron", "schedule": "*/5 * * * *"}, fire, c, first_due=c.now() - 3600)
    t.RESTART_BASE_S = 0.01
    t.start()
    await c.settle()
    await asyncio.sleep(0.1)  # the guard restarts the crashed trigger
    await c.settle()
    assert len(events) == 1
    await c.advance(300)
    assert len(events) == 2
    await t.stop()


async def _post(port, path, token=None, body="{}", header="X-K3-Token"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    h = f"{header}: {token}\r\n" if token else ""
    w.write(f"POST {path} HTTP/1.1\r\nHost: x\r\n{h}Content-Length: {len(body)}\r\n\r\n{body}".encode())
    await w.drain()
    data = await r.read()
    w.close()
    return int(data.split(b" ", 2)[1])


async def test_webhook_auth():
    events, fire = collector()
    srv = WebhookServer(0)
    port = await srv.start()
    srv.register("abc", "s3cret", fire)
    assert await _post(port, "/hook/abc", "wrong") == 401
    assert await _post(port, "/hook/abc") == 401
    assert events == []
    assert await _post(port, "/hook/zzz", "s3cret") == 404
    assert await _post(port, "/hook/abc", "s3cret", '{"a":1}') == 202
    assert await _post(port, "/hook/abc", "Bearer wrong", header="Authorization") == 401
    assert await _post(port, "/hook/abc", "Bearer s3cret", header="Authorization") == 202
    await until(lambda: len(events) >= 2)  # the action runs in the background after the 202
    assert events[0]["event"] == "webhook" and json.loads(events[0]["body"]) == {"a": 1}
    await srv.stop()


def mgr_for(tmp_path, runner=None, **kw):
    c, db = clock(), make_db(tmp_path)
    r = runner or FakeRunner()
    return c, db, r, AutomationManager(db, r, c, **kw)


def test_render():
    assert render("changed {{path}} / {{nope}}", {"path": "a.py"}) == "changed a.py / {{nope}}"


def test_render_shell_quotes_trigger_data(tmp_path):
    """Trigger data (file names, commit subjects, webhook bodies) went raw into the `shell` command: injection."""
    import subprocess

    evil = 'x\'; touch pwned; echo "$(id)" `id` $HOME'
    cmd = render("printf '%s|' {{path}} \"$K3_PATH\" {{nope}}", {"path": evil, "bad-key": "y"}, shell=True)
    out = subprocess.run(["bash", "-c", cmd], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert out == f"{evil}|{evil}|{{{{nope}}}}|" and not (tmp_path / "pwned").exists()


async def test_manager_actions_policy_and_events(tmp_path):
    c, db, r, m = mgr_for(tmp_path)
    for bad in (
        {"trigger": {"type": "nope"}, "action": {"type": "notify", "text": "x"}},
        {"trigger": {"type": "idle"}, "action": {"type": "notify", "text": "x"}},
        {"trigger": {"type": "net_state"}, "action": {"type": "shell"}},
        {"trigger": {"type": "webhook"}, "action": {"type": "notify", "text": "x"}},  # webhooks disabled
    ):
        try:
            m.add(name="x", **bad)
            raise AssertionError("expected AutomationError")
        except AutomationError:
            pass
    a = m.add(name="net", trigger={"type": "net_state"}, action={"type": "notify", "text": "back online"})
    s = m.add(
        name="needs",
        trigger={"type": "session_event", "event": "needs_input"},
        action={"type": "shell", "command": "echo {{session}}"},
        policy={"cooldown_s": 60},
        cwd="/tmp",
    )
    p = m.add(name="p", trigger={"type": "net_state"}, action={"type": "prompt", "prompt": "check {{event}}"})
    g = m.add(name="g", trigger={"type": "net_state"}, action={"type": "goal", "objective": "fix it"})
    m.net_change(True, True)  # no transition
    await c.settle()
    assert r.notes == []
    m.net_change(False, True)  # back online
    await until(lambda: r.notes and r.prompts and r.goals)
    assert r.notes[0][0] == "back online" and "check net_online" in r.prompts[0]["prompt"] and r.goals == ["fix it"]
    m.session_event("s1", "completed")
    m.session_event("s1", "needs_input", origin="automation")  # our own runs never trigger
    await c.settle()
    assert r.shells == []
    m.session_event("s1", "needs_input")
    await until(lambda: r.shells)
    assert r.shells[0][0].splitlines()[-1] == "echo s1" and r.shells[0][1] == "/tmp"
    m.session_event("s2", "needs_input")  # cooldown
    await c.settle()
    assert len(r.shells) == 1
    await c.advance(61)
    m.session_event("s2", "needs_input")
    await until(lambda: len(r.shells) == 2)
    assert db.get("automations", s["id"])["fire_count"] == 2
    assert (await m.test(a["id"])).startswith("Test run completed")
    await m.pause(p["id"])
    n = len(r.prompts)
    m.net_change(False, True)
    await c.settle()
    assert len(r.prompts) == n
    await m.resume(p["id"])
    assert await m.remove(g["id"]) and m.find(g["id"]) is None
    await m.stop()


async def test_manager_failed_shell_notifies_and_rate_limit(tmp_path):
    class R(FakeRunner):
        async def run_shell(self, command, cwd):
            return 2, "boom"

    c, db, r, m = mgr_for(tmp_path, R())
    a = m.add(
        name="sh",
        trigger={"type": "net_state"},
        action={"type": "shell", "command": "false"},
        policy={"max_per_hour": 1},
    )
    await m.fire(a["id"], {"event": "x"})
    await m.fire(a["id"], {"event": "x"})  # rate limited
    assert db.get("automations", a["id"])["fire_count"] == 1
    assert db.runs(a["id"])[0]["status"] == "failed" and any("failed" in n[0] for n in r.notes)
    await m.stop()


async def test_manager_file_trigger_and_webhook_end_to_end(tmp_path):
    c, db, r, m = mgr_for(tmp_path, webhook_port=0)
    await m.start()
    w = m.add(name="hook", trigger={"type": "webhook"}, action={"type": "notify", "text": "hooked {{body}}"})
    token = w["trigger"]["token"]
    assert await _post(m.webhook.port, f"/hook/{w['id']}", "nope") == 401
    assert await _post(m.webhook.port, f"/hook/{w['id']}", token, "hi") == 202
    await until(lambda: bool(r.notes))
    assert r.notes[0][0] == "hooked hi"
    m.add(
        name="files",
        trigger={"type": "file_change", "glob": "*.txt", "debounce": 0.05},
        action={"type": "notify", "text": "changed {{path}}"},
        cwd=str(tmp_path),
    )
    await asyncio.sleep(0.4)
    (tmp_path / "t.txt").write_text("x")
    await until(lambda: any("changed t.txt" in n[0] for n in r.notes), tries=300)
    await m.stop()


async def test_automations_survive_restart(tmp_path):
    c, db, r, m = mgr_for(tmp_path)
    a = m.add(name="nightly", trigger={"type": "cron", "schedule": "0 2 * * *"}, action={"type": "notify", "text": "n"})
    await m.start()
    await m.stop()
    due = db.get("automations", a["id"])["next_run_at"]
    c._now = due + 600  # daemon was down through 02:00
    m2 = AutomationManager(db, r, c)
    await m2.start()
    await c.settle()
    assert len(r.notes) == 1 and db.get("automations", a["id"])["fire_count"] == 1
    await m2.stop()


def test_suggestions_dedup_latch(tmp_path):
    db = make_db(tmp_path)
    s = Suggestions(db)
    first = s.suggest()
    assert len(first) == 4 and {x["dedup_key"] for x in first} >= {"catalog:nightly-tests"}
    assert len(s.suggest()) == 4  # idempotent: no duplicates
    assert s.dismiss("1")
    left = s.suggest()
    assert len(left) == 3 and all(x["dedup_key"] != first[0]["dedup_key"] for x in left)
    assert s.add(title="again", description="", source="catalog", spec={}, dedup_key=first[0]["dedup_key"]) is None
    acc = s.mark_accepted(left[0]["id"])
    assert acc and s.mark_accepted(left[0]["id"]) is None  # latched
    assert len(s.suggest()) == 2
    # a fresh instance on the same db keeps the latch
    assert len(Suggestions(db).suggest()) == 2
    assert not s.dismiss("nope")


async def test_result_type_unused():
    assert RunResult("completed").status == "completed"


async def test_webhook_action_longer_than_the_request_timeout_is_not_killed(monkeypatch):
    """The whole action used to run inside the request's wait_for(10 s): a real agent turn was cancelled mid-flight
    and the sender (GitHub, CI) got a 400 and retried it."""
    from k3code.automation import webhook

    monkeypatch.setattr(webhook, "READ_TIMEOUT_S", 0.2)
    finished = []

    async def slow(event):
        await asyncio.sleep(0.6)  # 3x the request timeout
        finished.append(event["body"])

    srv = WebhookServer(0)
    port = await srv.start()
    srv.register("abc", "s3cret", slow)
    assert await _post(port, "/hook/abc", "s3cret", "payload") == 202  # answered at once, not after 0.6 s
    assert finished == []
    await until(lambda: finished == ["payload"], tries=300)
    await srv.stop()


async def test_webhook_stop_cancels_running_actions():
    started, cancelled = asyncio.Event(), []

    async def forever(event):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    srv = WebhookServer(0)
    port = await srv.start()
    srv.register("abc", "s3cret", forever)
    assert await _post(port, "/hook/abc", "s3cret") == 202
    await asyncio.wait_for(started.wait(), 5)
    await srv.stop()
    assert cancelled == [True]
