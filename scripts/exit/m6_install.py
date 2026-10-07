"""M6 exit checks: fresh-container install, --from-bundle, resumable setup, update rollback, upstream sync dry run."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import REPO, emit, run, tail  # noqa: E402

LOGS = REPO / "scripts" / "exit" / "rows" / "logs"
LOGS.mkdir(parents=True, exist_ok=True)
CORE = REPO / "core"
IMAGE = "registry.fedoraproject.org/fedora:44"

INNER = r"""set -u
dnf -y -q install git curl gcc tar gzip xz which findutils procps-ng ca-certificates >/tmp/dnf.log 2>&1 \
  || { echo "DNF_FAIL"; tail -5 /tmp/dnf.log; exit 90; }
mkdir -p /tmp/k3code && cd /src && tar --exclude=.git --exclude=node_modules --exclude=.venv --exclude=__pycache__ \
  --exclude=.claude --exclude=.pytest_cache --exclude=.ruff_cache -cf - . | tar -xf - -C /tmp/k3code
cd /tmp/k3code
export HOME=/root PATH=/root/.local/bin:$PATH
T0=$(date +%s)
sh install/install.sh --from-source --yes --no-setup; rc=$?
T1=$(date +%s); echo "INSTALL_RC=$rc INSTALL_SECS=$((T1-T0))"
[ $rc = 0 ] || exit 91
k3code setup --non-interactive --answers install/answers.sample.yaml --no-probe; rc=$?
T2=$(date +%s); echo "SETUP_RC=$rc SETUP_SECS=$((T2-T1)) TOTAL_SECS=$((T2-T0))"
k3code doctor; echo "DOCTOR_RC=$?"
"""


def kenv(home: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("K3CODE_", "K3_"))}
    env.update(HOME=str(home), K3CODE_HOME=str(home / ".k3code"), K3CODE_DATA=str(home / "data"), **extra)
    return env


def k3(home: Path, *args: str, **kw: object) -> tuple[int, str]:
    return run(["uv", "run", "--project", str(CORE), "--quiet", "k3code", *args], cwd=home, env=kenv(home), **kw)  # type: ignore[arg-type]


def fresh_install() -> None:
    how = f"podman run {IMAGE}: dnf basics, cp /src to /tmp/k3code, install.sh --from-source --yes --no-setup, k3code setup --non-interactive --no-probe, k3code doctor"
    if not shutil.which("podman"):
        return emit("M6", "Fresh install in a fresh Fedora 44 container", how, "PENDING", "podman not installed",
                    "run scripts/exit/m6_install.py on a host with podman")
    inner = Path(tempfile.mkdtemp(prefix="m6inner-")) / "inner.sh"
    inner.write_text(INNER)
    log = LOGS / "m6_container.log"
    with open(log, "w") as f:
        try:
            p = subprocess.run(["podman", "run", "--rm", "-v", f"{REPO}:/src:ro,Z", "-v", f"{inner}:/inner.sh:ro,Z",
                                IMAGE, "sh", "/inner.sh"], stdout=f, stderr=subprocess.STDOUT, timeout=2400, check=False)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = 124
    text = log.read_text(errors="replace")
    m = re.search(r"TOTAL_SECS=(\d+)", text)
    srcs = re.search(r"INSTALL_SECS=(\d+)", text)
    dr = re.search(r"DOCTOR_RC=(\d+)", text)
    ev = f"podman rc={rc}; " + (f"install {srcs.group(1)}s, install+setup {m.group(1)}s; " if m and srcs else "") + tail(text, 5)
    setup_ok = "SETUP_RC=0" in text and "INSTALL_RC=0" in text
    if m and setup_ok and dr:
        secs = int(m.group(1))
        # The sample answers point at a dummy gateway (127.0.0.1:9) and the container has no real API keys, so the
        # doctor's provider and api-keys checks fail by construction. Every other check must pass.
        after_setup = text.split("SETUP_RC=")[-1]
        failed = re.findall(r"^[✗x] ([^:\s]+)", after_setup, re.M)
        unexpected = [f for f in failed if not (f.startswith("provider") or f == "api-keys")]
        ev += f"\ndoctor failures: {failed or 'none'}; unexpected: {unexpected or 'none'}"
        ok = secs < 600 and not unexpected
        emit("M6", "Fresh install in a fresh Fedora 44 container (<10 min incl. setup, doctor passes)", how,
             "PASS" if ok else "FAIL", ev)
    elif re.search(r"(Could not resolve|Failed to download|curl: \(|Temporary failure|Cannot download|No route)", text) or "DNF_FAIL" in text:
        emit("M6", "Fresh install in a fresh Fedora 44 container", how, "PENDING", ev,
             "rerun scripts/exit/m6_install.py with working network to the container (dnf, astral.sh, nodejs.org, go.dev)")
    else:
        emit("M6", "Fresh install in a fresh Fedora 44 container", how, "FAIL", ev)


def bundle_restore() -> None:
    how = "temp HOME A: setup+session+`k3code export`; temp HOME B: `install.sh --from-source --from-bundle` (real venv, no tui/go build), check config+session restored, secrets redacted"
    sys.path.insert(0, str(CORE / "src"))
    with tempfile.TemporaryDirectory(prefix="m6b-") as t:
        a, b = Path(t, "a"), Path(t, "b")
        a.mkdir(); b.mkdir()
        rc, out = k3(a, "setup", "--non-interactive", "--answers", str(REPO / "install/answers.sample.yaml"), "--no-probe")
        if rc != 0:
            return emit("M6", "--from-bundle restore", how, "FAIL", "setup in home A: " + out)
        code = ("from k3code.gateway.sessions import SessionStore;from pathlib import Path;import os;"
                "s=SessionStore(Path(os.environ['K3CODE_HOME'])/'sessions.db');x=s.create(title='m6-bundle-session',cwd=os.getcwd());print(x.session_id)")
        rc, sid = run(["uv", "run", "--project", str(CORE), "--quiet", "python", "-c", code], cwd=a, env=kenv(a))
        sid = sid.strip().splitlines()[-1] if rc == 0 and sid.strip() else ""
        bundle = Path(t, "x.k3bundle")
        rc, out = k3(a, "export", str(bundle), "--all")
        if rc != 0 or not bundle.exists():
            return emit("M6", "--from-bundle restore", how, "FAIL", "export: " + out)
        env = kenv(b, K3_SKIP_TUI="1", K3_SKIP_GO="1", PATH=f"{Path.home() / '.local/bin'}:{os.environ['PATH']}")
        rc, out = run(["sh", str(REPO / "install/install.sh"), "--from-source", "--from-bundle", str(bundle), "--yes",
                       "--no-setup"], cwd=b, env=env, timeout=900)
        cfg = b / ".k3code" / "config.yaml"
        env_k = b / "data" / "current" / "venv" / "bin" / "k3code"
        ok = rc == 0 and cfg.is_file() and bool(sid)
        secret = cfg.is_file() and "sk-SECRET" in cfg.read_text()
        listing = ""
        if env_k.exists():
            _, listing = run([str(env_k), "stats", "--json"], env=env)  # sanity that the installed CLI runs
        has_sess = False
        if ok:
            chk = ("from k3code.gateway.sessions import SessionStore;from pathlib import Path;import os;"
                   "print([x.title for x in SessionStore(Path(os.environ['K3CODE_HOME'])/'sessions.db').list()])")
            _, o2 = run([str(b / "data/current/venv/bin/python"), "-c", chk], env=env)
            has_sess = "m6-bundle-session" in o2
            listing = o2
        emit("M6", "--from-bundle restore (config + sessions restored, secrets not in bundle)", how,
             "PASS" if ok and has_sess and not secret else "FAIL",
             f"install rc={rc}, config restored={cfg.is_file()}, secret leaked={secret}, sessions in B: {listing.strip()[:150]}\n{tail(out, 3)}")


def interrupted_setup() -> None:
    how = "real CLI: interactive `k3code setup` killed with SIGINT mid-step (stdin held open), then re-run with --answers; plus pytest test_resume_after_interrupt"
    with tempfile.TemporaryDirectory(prefix="m6s-") as t:
        h = Path(t)
        # The real wizard needs a terminal (prompt_toolkit ignores piped stdin), so drive it through a pty:
        # press Enter (the defaults) one prompt at a time and send Ctrl-C as soon as two steps are SAVED.
        import pexpect

        out1 = ""
        state_files: list[Path] = []
        saved = 0
        child = pexpect.spawn("uv", ["run", "--project", str(CORE), "--quiet", "k3code", "setup", "--no-probe"],
                              cwd=str(h), env=kenv(h), dimensions=(40, 140), encoding="utf-8", codec_errors="replace",
                              timeout=5)
        try:
            deadline = time.time() + 180
            while time.time() < deadline and child.isalive():
                try:
                    out1 += child.read_nonblocking(65536, timeout=0.8)
                except pexpect.TIMEOUT:
                    pass
                except pexpect.EOF:
                    break
                child.send("\r")
                state_files = list(h.rglob("setup_state.json"))
                if state_files:
                    try:
                        saved = len(json.loads(state_files[0].read_text()).get("completed", []))
                    except ValueError:
                        saved = 0
                if saved >= 2:
                    break
            child.sendintr()
            time.sleep(2)
            try:
                out1 += child.read_nonblocking(65536, timeout=3)
            except Exception:  # noqa: BLE001
                pass
            child.close(force=True)
        except Exception as e:  # noqa: BLE001
            child.close(force=True)
            out1 += f"{type(e).__name__}: {e}"
        rc_int = child.exitstatus if child.exitstatus is not None else (128 + (child.signalstatus or 0))

        class _P:  # keep the evidence line below unchanged
            returncode = rc_int

        p = _P()
        interrupted = "Interrupted; progress saved" in out1 or p.returncode in (130, -2, 2, 128 + 2)
        rc2, out2 = k3(h, "setup", "--non-interactive", "--answers", str(REPO / "install/answers.sample.yaml"), "--no-probe")
        resumed = "Resuming at step" in out2
        cfg = (h / ".k3code" / "config.yaml").is_file()
        rc3, out3 = run(["uv", "run", "--quiet", "pytest", "-q", "--color=no", "-p", "no:cacheprovider", "tests/test_setup.py", "-k", "resume"], cwd=CORE, timeout=300)
        ok = interrupted and saved >= 2 and resumed and rc2 == 0 and cfg and rc3 == 0
        emit("M6", "Interrupted setup resumes", how, "PASS" if ok else "FAIL",
             f"sigint rc={p.returncode} interrupted={interrupted} steps saved before the interrupt={saved}; "
             f"rerun rc={rc2} resumed={resumed} config={cfg}; pytest rc={rc3}: {tail(out3, 1)}\n{tail(out2, 2)}")


def broken_update() -> None:
    how = "real CLI `k3code update --from-source --yes` in temp K3CODE_DATA: staged version fails its smoke test -> current untouched; `update --rollback`; plus pytest test_update.py (daemon-unhealthy rollback)"
    with tempfile.TemporaryDirectory(prefix="m6u-") as t:
        h = Path(t, "home"); h.mkdir()
        data = h / "data"
        def mk(ver: str, ok: bool) -> None:
            exe = data / "versions" / ver / "venv" / "bin" / "k3code"
            exe.parent.mkdir(parents=True)
            exe.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "k3code, version %s"; exit %d; fi\n'
                           'echo \'{"summary":{"ok":1,"warn":0,"fail":0},"checks":[]}\'\n' % (ver, 0 if ok else 3))
            exe.chmod(0o755)
        mk("1.0.0", True); mk("2.0.0", False)
        (data / "current").symlink_to(data / "versions" / "1.0.0")
        # source checkout with a shim installer that "stages" the broken 2.0.0 and a remote to pull from
        origin = Path(t, "origin.git"); src = Path(t, "src")
        run(["git", "init", "-q", "--bare", "-b", "main", str(origin)])
        run(["git", "init", "-q", "-b", "main", str(src)])
        (src / "install").mkdir()
        (src / "install" / "install.sh").write_text("#!/bin/sh\necho 2.0.0\n")
        for c in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                  ["remote", "add", "origin", str(origin)], ["push", "-q", "-u", "origin", "main"]):
            run(["git", *c], cwd=src)
        (data / "source_path").write_text(str(src))
        rc, out = k3(h, "update", "--from-source", "--yes")
        cur = (data / "current").resolve().name
        stayed = cur == "1.0.0" and rc != 0 and "smoke test failed" in out
        # now make 2.0.0 healthy-looking at the smoke test but force a manual rollback path
        mk_fix = data / "versions" / "2.0.0" / "venv" / "bin" / "k3code"
        mk_fix.write_text(mk_fix.read_text().replace("exit 3", "exit 0")); 
        rc2, out2 = k3(h, "update", "--from-source", "--yes")
        switched = (data / "current").resolve().name == "2.0.0"
        rc3, out3 = k3(h, "update", "--rollback")
        back = (data / "current").resolve().name == "1.0.0"
    rc4, out4 = run(["uv", "run", "--quiet", "pytest", "-q", "--color=no", "-p", "no:cacheprovider", "tests/test_update.py"], cwd=CORE, timeout=300)
    ok = stayed and switched and back and rc4 == 0
    emit("M6", "A broken update rolls back (broken version never becomes current; rollback restores previous)", how,
         "PASS" if ok else "FAIL",
         f"broken: rc={rc} current stayed 1.0.0={stayed} ({tail(out, 1)}); fixed update switched={switched}; rollback={back} ({tail(out3, 1)}); pytest rc={rc4}: {tail(out4, 1)}")


def upstream_sync() -> None:
    crit = ("Upstream sync: mergeable subtrees have <10 conflicting files; the Hermes TUI is a documented frozen fork")
    how = ("scripts/sync-upstream.sh --dry-run: git fetch tuios + hermes-agent upstream HEAD into temp bare repos, 3-way "
           "blob diff vs recorded base commits; the frozen fork (hermes-agent:tui) is reported, not counted; "
           "docs/UPSTREAM.md must document the policy and the cherry-pick procedure")
    rc, out = run(["sh", str(REPO / "scripts/sync-upstream.sh"), "--dry-run"], timeout=1500)
    (LOGS / "m6_sync.log").write_text(out)
    if rc == 3:
        return emit("M6", crit, how, "PENDING", out, "rerun scripts/sync-upstream.sh --dry-run with network access to github.com")
    doc = REPO / "docs" / "UPSTREAM.md"
    doc_ok = doc.is_file() and all(w in doc.read_text() for w in ("Frozen fork", "Cherry-picking", "Merging TUIOS"))
    frozen_reported = "FROZEN FORK" in out
    emit("M6", crit, how, "PASS" if rc == 0 and doc_ok and frozen_reported else "FAIL",
         "\n".join(ln for ln in out.splitlines() if ln.startswith("SUBTREE")) or out)
    if not doc_ok:
        print("docs/UPSTREAM.md missing or incomplete")


if __name__ == "__main__":
    only = sys.argv[1:] or ["sync", "bundle", "resume", "update", "fresh"]
    for name, fn in (("sync", upstream_sync), ("bundle", bundle_restore), ("resume", interrupted_setup),
                     ("update", broken_update), ("fresh", fresh_install)):
        if name in only:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                emit("M6", f"check {name}", "m6_install.py", "FAIL", f"{type(e).__name__}: {e}")
