"""Home hygiene, last file in the suite (alphabetical order): nothing may have written under the real home.

The detector lives in conftest.py (``_audit``). This test also proves the detector fires, so a silent detector cannot
pass for a clean suite. The probe only calls ``os.open`` on a path whose parent does not exist: the audit event fires,
nothing is created.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def test_real_home_sentinel_untouched(home_sentinel, tmp_path):
    assert len(home_sentinel.writes) == 0, "a test wrote under the real home (paths withheld, see conftest)"
    assert Path.home() != home_sentinel.real_home and str(Path.home()).startswith(str(tmp_path.parent))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert os.environ[var].startswith(str(tmp_path.parent)), var

    probe = home_sentinel.real_home / "k3code-sentinel-probe" / "never-created"
    # a link that points into the real home writes only the link (test_installer.py's PATH link farms do this)
    os.symlink(probe, tmp_path / "link-into-home")
    assert len(home_sentinel.writes) == 0, "a link pointing into the real home was counted as a write there"
    with contextlib.suppress(FileNotFoundError):
        os.open(probe, os.O_WRONLY | os.O_CREAT)
    assert len(home_sentinel.writes) == 1, "the detector did not see a write under the real home"
    home_sentinel.writes.clear()  # the probe was refused by the OS; it is not a leak
