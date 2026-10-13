"""The prompt prefix a provider caches (system prompt, then the tools array) is byte-stable.

#69/#70 worried that set/dict iteration could reorder it. Sets are only used for membership; these tests pin that,
across interpreter runs with different string hash seeds (which is what reorders a set)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PROBE = """
import json, sys
from pathlib import Path
from k3code.config import Settings
from k3code.prompting import build_system_prompt
from k3code.tools import build_registry

reg = build_registry()
plain = [s.name for s in reg.specs()]
deferred = sorted(reg._deferred)
reg.activate(deferred[::-1])
reverse = [s.name for s in reg.specs()]
reg2 = build_registry()
reg2.activate(deferred)
forward = [s.name for s in reg2.specs()]
prompt = build_system_prompt("BASE", cwd=Path(sys.argv[1]), config=Settings())
print(json.dumps({"plain": plain, "reverse": reverse, "forward": forward, "prompt": prompt}))
"""


def _run(seed: str, cwd: Path, home: Path) -> dict:
    env = {"PYTHONHASHSEED": seed, "K3CODE_HOME": str(home), "HOME": str(home), "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, "-c", _PROBE, str(cwd)], env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.splitlines()[-1])


def test_prompt_and_tools_array_do_not_depend_on_hash_seed_or_activation_order(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "K3CODE.md").write_text("# memory\n- one\n- two\n")
    runs = [_run(seed, cwd, tmp_path / "home") for seed in ("0", "1", "2", "12345")]
    first = runs[0]
    for other in runs[1:]:
        assert other["prompt"] == first["prompt"]
        assert other["plain"] == first["plain"]
        assert other["forward"] == first["forward"]
    assert first["reverse"] == first["forward"]  # activating deferred tools in any order gives the same array
    assert [n for n in first["forward"] if n in first["plain"]] == first["plain"]  # activation only inserts
