"""Hatch build hook: put k3code's license files into the wheel.

LICENSE, NOTICE and LICENSES/ live at the repository root, outside core/, the project hatchling builds. A plain
``force-include`` of ``../LICENSE`` breaks the wheel that ``uv build`` makes from the sdist (``..`` is then a temporary
directory) and would also go into every editable install. This hook copies them into the wheel's ``k3code/`` package
for a standard wheel only, from the repository root in a checkout or from the sdist's own root (the sdist target
force-includes them, see pyproject.toml). A wheel without them fails to build: it would ship the vendored code without
its notices (issue #165). ``scripts/release/third_party_notices.py check-wheel`` checks the result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: What every k3code wheel carries, from the repository root, under k3code/ in the wheel.
LICENSE_PATHS = ("LICENSE", "NOTICE", "LICENSES")


class LicenseFilesHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel" or version != "standard":
            return  # editable installs point at the source tree, which has the files already
        root = Path(self.root)
        for name in LICENSE_PATHS:
            # core/ in a checkout (the files are one level up), the unpacked sdist (they are beside pyproject.toml)
            src = next((p for p in (root.parent / name, root / name) if p.exists()), None)
            if src is None:
                raise FileNotFoundError(
                    f"{name} is neither in {root.parent} nor in {root}: a k3code wheel must ship k3code's license "
                    "files (LICENSE, NOTICE, LICENSES/ at the repository root)"
                )
            build_data["force_include"][str(src)] = f"k3code/{name}"
