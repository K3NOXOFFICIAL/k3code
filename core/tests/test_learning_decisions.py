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
    assert scrub("api_key=hunter2") == "[redacted]"
