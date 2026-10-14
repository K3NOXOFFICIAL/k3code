"""K3NOX-496: a failing test never prints the secrets of the host that runs the suite.

``debugdump.secret_values()`` reads every secret-named variable of the environment. An assertion on its result printed
the whole list when it failed, so the host's real tokens reached the terminal, the CI log and any transcript quoting
it. Each test now sees no secret-named host variable (``_no_host_secrets`` in conftest.py), and the redaction tests
assert on their own fake values only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from k3code.debugdump import _SECRET_NAME

CORE = Path(__file__).resolve().parents[1]
#: The fake value of a host variable, built at run time so the secret scan has nothing to match.
HOST_TOKEN = "h0st" + "Only" + "Zq81xYw9Pq2L"

#: Plugins the inner run loads before the suite, each breaking redaction so that the redaction tests fail, and the
#: pytest options it runs with (`-vv` prints whole lists, `--showlocals` every local of a failing test).
BREAKS = {
    # `_ordinary` broken the way K3NOX-374 broke it, while the host has a secret-named variable: only hiding the
    # variable keeps it out of a failing test's locals
    "host-variable": ("from k3code import debugdump\n\ndebugdump._ordinary = lambda value: False\n", ["--showlocals"]),
    # a host value reached the result some other way, the test's own values went missing and two ordinary ones came in:
    # the assertions compare the test's own values only
    "host-value-in-result": (
        "from k3code import debugdump\n\n"
        "debugdump.secret_values = lambda config=None: "
        f"[{HOST_TOKEN!r}, '127.0.0.1', '/var/run/docker.sock']\n",
        [],
    ),
}


def test_a_test_sees_no_secret_named_host_variable():
    names = [n for n in os.environ if _SECRET_NAME.search(n)]  # names only: a failure must not print a value
    assert names == []


@pytest.mark.parametrize("break_", list(BREAKS))
def test_a_failing_redaction_test_does_not_print_a_host_secret(break_, tmp_path):
    plugin = tmp_path / "plugin" / "k3_break_redaction.py"
    plugin.parent.mkdir()
    source, options = BREAKS[break_]
    plugin.write_text(source)
    env = {n: v for n, v in os.environ.items() if not _SECRET_NAME.search(n)}  # the host's own never reach the run
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(plugin.parent), env.get("PYTHONPATH")]))
    if break_ == "host-variable":
        env["K3_FAKE_HOST_API_TOKEN"] = HOST_TOKEN
    tests = ["tests/test_debugdump_redaction.py", "tests/test_debugdump_redaction_gaps.py"]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-vv", *options, "-o", "addopts=", "-p", "no:cacheprovider"]
        + ["-p", "k3_break_redaction", f"--basetemp={tmp_path / 'inner'}", *tests],
        cwd=CORE,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    out = proc.stdout + proc.stderr
    summary = out.strip().splitlines()[-1] if out.strip() else ""
    assert proc.returncode == 1 and " failed" in summary, summary  # the break did make redaction tests fail
    leaked = HOST_TOKEN in out  # a bool: this assertion must not print the output it searched either
    assert not leaked, f"{break_}: a failing redaction test printed the host's secret"
