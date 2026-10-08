"""Shared helpers for exit checks. Each check calls emit() once per criterion; rows go to $EXIT_ROWS (JSONL)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATUSES = ("PASS", "FAIL", "PENDING")


def tail(text: str, n: int = 6, width: int = 300) -> str:
    lines = [ln.rstrip()[:width] for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def emit(milestone: str, criterion: str, how: str, status: str, evidence: str, close: str = "") -> None:
    """status in PASS/FAIL/PENDING; close = exact action that would close a PENDING (required for PENDING)."""
    assert status in STATUSES, status
    if status == "PENDING" and not close:
        raise ValueError("PENDING rows need a `close` action")
    row = {"milestone": milestone, "criterion": criterion, "how": how, "status": status,
           "evidence": tail(evidence, 6), "close": close}
    out = os.environ.get("EXIT_ROWS")
    line = json.dumps(row)
    if out:
        with open(out, "a") as f:
            f.write(line + "\n")
    print(f"[{status}] {milestone}: {criterion}", file=sys.stderr)


def run(cmd: str | list[str], cwd: Path | str | None = None, env: dict | None = None,
        timeout: int = 600) -> tuple[int, str]:
    """Run a command, return (rc, combined output). Never raises on failure/timeout."""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, f"TIMEOUT after {timeout}s\n{(e.stdout or b'')[-500:]!r}"


LIVE_DEFAULT_MODEL = "claude-sonnet-5-5"
LIVE_CHEAP_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_CLI_LABEL = ("live Claude Sonnet 5.5 (cheap tier: Haiku 4.5) via the claude-cli provider; "
                    "OmniRoute paused by owner")


def live_backend() -> dict:
    """Which model backs the live-model checks: {kind, ok, detail, label}.

    Default is the claude-cli provider (the owner's Claude Code login: Sonnet 5.5, cheap tier Haiku 4.5), because the
    owner paused all OmniRoute use on 2026-10-07. K3_ALLOW_OMNIROUTE=1 selects the original OmniRoute path.
    """
    if os.environ.get("K3_ALLOW_OMNIROUTE") == "1":
        ok, detail = omniroute_quota()
        return {"kind": "omniroute", "ok": ok, "detail": detail,
                "label": f"live OmniRoute {OMNI_AGENT_MODEL} (cheap tier auto/coding-cheap)"}
    import shutil

    path = shutil.which("claude")
    if not path:
        return {"kind": "claude-cli", "ok": False, "detail": "claude CLI not found on PATH", "label": CLAUDE_CLI_LABEL}
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "OMNIROUTE_"))}
    rc, out = run([path, "auth", "status"], env=env, timeout=30)
    try:
        logged = rc == 0 and json.loads(out).get("loggedIn") is True
    except json.JSONDecodeError:
        logged = False
    return {"kind": "claude-cli", "ok": logged, "label": CLAUDE_CLI_LABEL,
            "detail": "Claude Code login ok" if logged else f"Claude Code is not logged in ({tail(out, 1)})"}


# Build workers run on the owner's personal combo (owner decision 2026-10-07), but that combo prepends a "Who are you?" bootstrap
# prompt that makes a headless product agent stop and ask. The product's own live rows therefore use a plain combo.
OMNI_AGENT_MODEL = "auto/coding-manual"


def live_providers_yaml(backend: dict, omni_default: str | list[str] = OMNI_AGENT_MODEL,
                        omni_cheap: str = "auto/coding-cheap") -> str:
    """The `providers:` block of a k3code config for the live backend (no secrets, only the key variable name)."""
    if backend["kind"] == "omniroute":
        default = f"[{', '.join(omni_default)}]" if isinstance(omni_default, list) else omni_default
        return ("providers:\n  - {name: omniroute, kind: openai, base_url: 'http://<omniroute-host>:20128/v1', "
                f"api_key_env: OMNIROUTE_API_KEY, models: {{default: {default}, cheap: {omni_cheap}, "
                f"fast: {omni_cheap}}}}}\n")
    return (f"providers:\n  - {{name: claude-code, kind: claude-cli, models: {{default: {LIVE_DEFAULT_MODEL}, "
            f"strong: {LIVE_DEFAULT_MODEL}, cheap: {LIVE_CHEAP_MODEL}, fast: {LIVE_CHEAP_MODEL}}}}}\n")


def live_chat(backend: dict, messages: list[dict], max_tokens: int = 400) -> str:
    """One bare completion on the cheap tier of the live backend (for checks that need a model call, not a session)."""
    if backend["kind"] == "omniroute":
        import urllib.request

        req = urllib.request.Request(
            "http://<omniroute-host>:20128/v1/chat/completions",
            data=json.dumps({"model": "auto/coding-cheap", "max_tokens": max_tokens, "messages": messages}).encode(),
            headers={"Authorization": f"Bearer {os.environ['OMNIROUTE_API_KEY']}", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)["choices"][0]["message"]["content"]
    import asyncio

    from k3code.providers import ClaudeCliProvider
    from k3code.providers.types import Message

    async def go() -> str:
        provider = ClaudeCliProvider(name="claude-code")
        try:
            events = [e async for e in provider.stream(
                [Message(role=m["role"], content=m["content"]) for m in messages], [], LIVE_CHEAP_MODEL)]
        finally:
            await provider.aclose()
        done = events[-1].message
        return (done.content if done else "") or ""

    return asyncio.run(go())


def omniroute_quota() -> tuple[bool, str]:
    """(usable, detail). 429 'daily usage quota' -> (False, reset info). Never prints the key.

    Off by default: the owner paused all OmniRoute use on 2026-10-07 (the key's daily usage limit is
    shared by everything that uses it). Set K3_ALLOW_OMNIROUTE=1 to run the live-model checks; until
    then they stay PENDING and no request leaves the machine.
    """
    import urllib.error
    import urllib.request

    if os.environ.get("K3_ALLOW_OMNIROUTE") != "1":
        return False, "OmniRoute use paused by the owner (2026-10-07); live-model checks wait for K3_ALLOW_OMNIROUTE=1"
    key = os.environ.get("OMNIROUTE_API_KEY", "")
    if not key:
        return False, "OMNIROUTE_API_KEY not set"
    req = urllib.request.Request(
        "http://<omniroute-host>:20128/v1/chat/completions",
        data=json.dumps({"model": "auto/coding-cheap", "max_tokens": 5,
                         "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status == 200, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        return False, f"HTTP {e.code} Retry-After={e.headers.get('Retry-After')} {body}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
