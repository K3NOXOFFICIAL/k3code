#!/usr/bin/env python3
"""M0 exit checks: headless read/edit/bash task, failover on a blocked primary, vendor_check, ~/.hermes untouched."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chaoslib as cl  # noqa: E402
from lib import REPO, emit, omniroute_quota, run  # noqa: E402

M = "M0"
START = float(os.environ.get("EXIT_RUN_START") or time.time())
TMP = Path(tempfile.mkdtemp(prefix="exit-m0-"))
K3 = cl.k3code_bin()


def base_env(home: Path) -> dict[str, str]:
    (home / "h").mkdir(parents=True, exist_ok=True)
    e = dict(os.environ, K3CODE_HOME=str(home), HOME=str(home / "h"))
    e.pop("K3CODE_FAKE_PROVIDER", None)
    return e


def headless_fake() -> None:
    home, proj = TMP / "fake-home", TMP / "fake-proj"
    proj.mkdir(parents=True)
    (proj / "app.py").write_text('def greet():\n    return "hello"\n\nprint(greet())\n')
    step = lambda n, a: {"type": "tool_call", "id": n, "name": n, "when": "first", "arguments": a}  # noqa: E731
    (home).mkdir(parents=True)
    (home / "fake.json").write_text(json.dumps([
        step("read", {"path": "app.py"}),
        step("edit", {"path": "app.py", "old_string": '"hello"', "new_string": '"hello, k3"'}),
        step("bash", {"command": "python3 app.py > out.txt"}),
        {"type": "text", "text": "DONE: edited app.py and ran it", "when": "after_tool"},
        {"type": "usage", "prompt_tokens": 5, "completion_tokens": 2}]))
    (home / "config.yaml").write_text(
        "providers:\n  - {name: fake, kind: openai, base_url: 'http://fake', api_key_env: PATH, models: {default: m}}\n"
        "reliability: {flags: {netwatch: false}}\n")
    env = base_env(home)
    env["K3CODE_FAKE_PROVIDER"] = str(home / "fake.json")
    rc, out = run([K3, "-p", "Read app.py, change the greeting, run it", "--permission", "yolo"], cwd=proj, env=env,
                  timeout=180)
    ok = rc == 0 and "hello, k3" in (proj / "app.py").read_text() and (proj / "out.txt").exists() \
        and "hello, k3" in (proj / "out.txt").read_text()
    emit(M, "k3code finishes a headless read/edit/bash task (mechanics)",
         "`k3code -p ... --permission yolo` in a temp project with K3CODE_FAKE_PROVIDER scripting read+edit+bash; "
         "asserts app.py edited and out.txt produced by the bash step", "PASS" if ok else "FAIL",
         f"rc={rc} app.py={(proj / 'app.py').read_text()!r} out.txt={(proj / 'out.txt').read_text() if (proj / 'out.txt').exists() else 'MISSING'!r}\n{out}")


def headless_live() -> None:
    crit = "k3code finishes a read/edit/bash task through OmniRoute (live model)"
    ok, detail = omniroute_quota()
    if not ok:
        emit(M, crit, "live `k3code -p` through OmniRoute", "PENDING", f"OmniRoute unusable now: {detail}",
             "After the OmniRoute daily quota resets (see Retry-After above), rerun `scripts/exit/run_all.sh --only m0`")
        return
    home, proj = TMP / "live-home", TMP / "live-proj"
    home.mkdir(parents=True)
    proj.mkdir(parents=True)
    (proj / "app.py").write_text('def greet():\n    return "hello"\n\nprint(greet())\n')
    (home / "config.yaml").write_text(
        'providers:\n  - {name: omniroute, kind: openai, base_url: "http://<omniroute-host>:20128/v1", '
        "api_key_env: OMNIROUTE_API_KEY, models: {default: [auto/coding-manual, auto/best-coding], "
        "cheap: auto/coding-cheap}}\n")
    rc, out = run([K3, "-p", "Read app.py, edit greet() to return 'hello, k3', then run it with bash and "
                   "tell me the output.", "--permission", "yolo"], cwd=proj, env=base_env(home), timeout=300)
    done = rc == 0 and "hello, k3" in (proj / "app.py").read_text()
    emit(M, crit, "`k3code -p` live via OmniRoute auto/coding-* in a temp project", "PASS" if done else "FAIL",
         f"rc={rc} app.py={(proj / 'app.py').read_text()!r}\n{out}")


def failover() -> None:
    crit = "Blocking the primary fails over to the secondary"
    procs = cl.Procs()
    try:
        up = 19300 + os.getpid() % 500
        cl.start_fake_upstream(procs, up)
        home, proj = TMP / "fo-home", TMP / "fo-proj"
        home.mkdir(parents=True)
        proj.mkdir(parents=True)
        (home / "config.yaml").write_text(
            "providers:\n"
            "  - {name: primary, kind: openai, base_url: 'http://127.0.0.1:9/v1', api_key_env: K, models: {default: m}}\n"
            f"  - {{name: secondary, kind: openai, base_url: 'http://127.0.0.1:{up}/v1', api_key_env: K, "
            "models: {default: m}}\n"
            "reliability: {flags: {netwatch: false}}\n")
        env = base_env(home)
        env["K"] = "x"
        t0 = time.monotonic()
        rc, out = run([K3, "-p", "RUN[echo failover-ok > f.txt]", "--permission", "yolo"], cwd=proj, env=env,
                      timeout=180)
        dt = time.monotonic() - t0
        ok = rc == 0 and (proj / "f.txt").exists() and "failover" in out.lower()
        emit(M, crit, "primary = unreachable port 9, secondary = local fake upstream; headless run must complete "
             "via the secondary and log the failover", "PASS" if ok else "FAIL", f"rc={rc} {dt:.1f}s\n{out}")
    finally:
        procs.close()


def vendor() -> None:
    rc, out = run([sys.executable, str(REPO / "scripts" / "vendor_check.py")], cwd=REPO)
    emit(M, "vendor_check passes", "`python3 scripts/vendor_check.py`", "PASS" if rc == 0 else "FAIL", out)


def hermes_untouched() -> None:
    hermes = Path.home() / ".hermes"
    if not hermes.exists():
        emit(M, "~/.hermes untouched", "find ~/.hermes -newermt @run_start", "PASS", "~/.hermes does not exist")
        return
    rc, out = run(["find", str(hermes), "-newermt", f"@{int(START)}", "-not", "-path", "*/logs/*",
                   "-not", "-path", "*/sessions/*", "-not", "-path", "*/state.db*", "-not", "-name", "*.lock",
                   "-type", "f"], timeout=120)
    files = [ln for ln in out.splitlines() if ln.strip()]
    emit(M, "~/.hermes untouched (no files newer than the run start)",
         "`find ~/.hermes -newermt @<run start> -type f` (excluding the live Hermes agent's own logs/sessions/state "
         "db/locks, which another process legitimately writes)",
         "PASS" if not files else "FAIL",
         f"{len(files)} file(s) newer than run start\n" + "\n".join(files[:5]))


for fn in (headless_fake, headless_live, failover, vendor, hermes_untouched):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        import traceback
        emit(M, fn.__name__, "crashed", "FAIL", traceback.format_exc())
shutil.rmtree(TMP, ignore_errors=True)
