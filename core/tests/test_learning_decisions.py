import json

from k3code.learning.decisions import KINDS, DecisionLog, project_id, scrub
from learn_helpers import FakeClock


def test_migrates_jsonl_once(tmp_path):
    rows = [
        {"session": "s1", "tool": "bash", "pattern": "npm test *", "choice": "once", "cwd": "/x", "ts": 5.0},
        {"session": "s1", "tool": "exit_plan", "pattern": "*", "choice": "deny", "cwd": "/x", "ts": 6.0},
    ]
    (tmp_path / "decisions.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    log = DecisionLog(tmp_path)
    assert not (tmp_path / "decisions.jsonl").exists() and (tmp_path / "decisions.jsonl.migrated").exists()
    a = log.query("approval")
    assert a[0]["subject"] == "npm test *" and a[0]["detail"]["tool"] == "bash" and a[0]["ts"] == 5.0
    assert log.query("plan")[0]["choice"] == "deny"
    assert DecisionLog(tmp_path).count() == 2  # reopening does not re-import
    assert (tmp_path / "learning" / "decisions.db").is_file()


def test_records_every_kind_with_cwd_project_and_ts(tmp_path):
    clock = FakeClock()
    log = DecisionLog(tmp_path, clock)
    work = tmp_path / "work"
    work.mkdir()
    samples = {
        "approval": dict(subject="npm test *", choice="always", detail={"tool": "bash"}),
        "model_switch": dict(subject="a -> b", choice="b", detail={"from": "a", "to": "b", "reason": "slow"}),
        "proposal": dict(subject="x", choice="dismiss", detail={"kind": "improvement"}),
        "plan": dict(subject="exit_plan", choice="deny", detail={"edited": True, "has_verification": False}),
        "interrupt": dict(subject="bash", choice="stop"),
        "undo": dict(subject="config.yaml", choice="config-rollback"),
        "scope": dict(subject="override", choice="small"),
        "config": dict(subject="update-config", choice="apply"),
        "auto_apply": dict(subject="clip tool results", choice="applied", actor="auto", detail={"experiment": "x1"}),
        "tool_error": dict(subject="npm: exit <n>: sh: npm: not found", choice="exit 127", detail={"tool": "bash"}),
    }
    assert set(samples) == set(KINDS)
    for kind, kw in samples.items():
        log.record(kind, session="s", cwd=str(work), **kw)
        clock.advance(1)
    assert log.count() == len(KINDS)
    row = log.query("model_switch")[0]
    assert row["detail"]["reason"] == "slow" and row["cwd"] == str(work) and row["ts"] > 1e9
    assert row["project"].startswith("path:")
    assert log.query("plan")[0]["detail"]["edited"] is True


def test_project_id_git_remote_strips_credentials(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "https://user:tok@github.com/acme/app.git"], check=True
    )
    pid = project_id(tmp_path)
    assert pid == "git:github.com/acme/app" and "tok" not in pid


def test_secrets_scrubbed_from_log(tmp_path):
    log = DecisionLog(tmp_path)
    log.record("config", subject="set key sk-abcdefghijklmnop", detail={"v": "Bearer abc.def"})
    row = log.query("config")[0]
    assert "sk-abc" not in row["subject"] and "abc.def" not in json.dumps(row["detail"])
    # scrub is now redact.scrub_text: the variable name stays, the value goes
    assert scrub("api_key=hunter2") == "api_key=<redacted>"


# A fake JWT, joined at run time so no committed line carries the literal for a secret scanner to flag.
_FAKE_JWT_SEGMENTS = ("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0In0", "c2lnbmF0dXJl")
SECRET_SHAPES = {
    "postgres://alice:hunter2pass@db.myapp.example/app": "hunter2pass",
    "deploy --password hunter2pass": "hunter2pass",
    "--password hunter2pass": "hunter2pass",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0abc\n-----END RSA PRIVATE KEY-----": "MIIEpAIBAAKCAQEA0abc",
    "token github_pat_11ABCDEFG0123456789_abcdefghijklmnop": "github_pat_11ABCDEFG",
    "slack xoxb-1234567890-abcdefghij": "xoxb-1234567890",
    "jwt " + ".".join(_FAKE_JWT_SEGMENTS): _FAKE_JWT_SEGMENTS[1],
}


def test_scrub_catches_every_secret_shape(tmp_path):
    for text, secret in SECRET_SHAPES.items():
        assert secret not in scrub(text), text
    log = DecisionLog(tmp_path)
    for text in SECRET_SHAPES:
        log.record("config", subject=text, detail={"cmd": text})
    dumped = json.dumps(log.query("config"))
    assert not [s for s in SECRET_SHAPES.values() if s in dumped]


def test_tool_error_sightings_count_one_signature_of_one_tool_in_one_project_inside_the_window(tmp_path):
    clock = FakeClock()
    log = DecisionLog(tmp_path, clock)
    sig = "npm: exit <n>: sh: <n>: npm: not found"
    now = clock()

    def add(ts, *, subject=sig, tool="bash", project="p", choice="exit 127", actor="user", **detail):
        log.record(
            "tool_error",
            subject=subject,
            choice=choice,
            detail={"tool": tool, **detail},
            ts=ts,
            project=project,
            actor=actor,
        )

    add(now - 15 * 86400, followup="`old` worked instead")  # older than the window
    add(now - 3 * 86400, project="other", followup="`other` worked instead")
    add(now - 3 * 86400, tool="read")
    add(now - 3 * 86400, subject="other signature")
    log.record("approval", subject=sig, detail={"tool": "bash"}, ts=now - 3 * 86400, project="p")  # not a tool_error
    since = now - 14 * 86400
    assert log.tool_error_sightings("p", "bash", sig, since, 3) == (0, "", "")

    add(now - 2 * 86400, choice="exit 1", followup="`pnpm test` worked instead")
    add(now - 86400, choice="exit 2", actor="auto")  # any actor counts; no retry noted on it
    add(now - 3600, choice="exit 127", followup="")
    seen = log.tool_error_sightings("p", "bash", sig, since, 10)
    assert seen.count == 3 and seen.choice == "exit 127"  # the newest row's class
    assert seen.followup == "`pnpm test` worked instead"  # the newest retry that was noted, not the newest row's
    assert log.tool_error_sightings("p", "bash", sig, since, 2).count == 2  # capped
    assert log.tool_error_sightings("p", "read", sig, since, 10).count == 1


def test_tool_error_sightings_use_the_composite_index_and_decode_no_row(tmp_path, monkeypatch):
    from k3code.learning import decisions

    log = DecisionLog(tmp_path)
    sig = "npm: exit <n>: sh: <n>: npm: not found"
    for i in range(300):
        log.record("tool_error", subject=f"s{i}", choice="exit 1", detail={"tool": "bash"}, project="p", ts=1000.0 + i)
    log.record(
        "tool_error", subject=sig, choice="exit 127", detail={"tool": "bash", "followup": "x"}, project="p", ts=1500.0
    )
    plan = " ".join(
        str(r[3])
        for r in log._db.execute(
            "EXPLAIN QUERY PLAN SELECT choice FROM decisions WHERE kind='tool_error' AND project=? AND subject=?"
            " AND ts>=? AND json_extract(detail, '$.tool')=? ORDER BY ts DESC, id DESC LIMIT 1",
            ("p", sig, 0.0, "bash"),
        )
    )
    assert "decisions_kind_project_subject_ts" in plan and "SCAN" not in plan
    monkeypatch.setattr(decisions, "json", None)  # any json.loads/dumps in the lookup would raise
    assert log.tool_error_sightings("p", "bash", sig, 0.0, 3) == (1, "exit 127", "x")


def test_an_older_database_gets_the_signature_index_when_it_is_opened(tmp_path):
    DecisionLog(tmp_path).close()
    import sqlite3

    db = sqlite3.connect(tmp_path / "learning" / "decisions.db")
    db.execute("DROP INDEX decisions_kind_project_subject_ts")
    db.commit()
    db.close()
    log = DecisionLog(tmp_path)
    names = {r[1] for r in log._db.execute("PRAGMA index_list(decisions)")}
    assert "decisions_kind_project_subject_ts" in names
