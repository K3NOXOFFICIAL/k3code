"""Third-party notices in the release assets (issue #165): scripts/release/third_party_notices.py and the wheel's
license files (core/hatch_build.py).

The generator tests use invented lockfiles and module caches under tmp_path; one test builds the real wheel with uv.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "release" / "third_party_notices.py"
MIT = "MIT License\n\nCopyright (c) 2020 Someone\n\nPermission is hereby granted, free of charge, to any person ..."
BSD = "Copyright 2009 The Go Authors.\n\nRedistribution and use in source and binary forms ... Neither the name ..."


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("third_party_notices", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look the module up while the class is created
    spec.loader.exec_module(mod)
    return mod


tpn = _load()


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=cwd, check=False, timeout=600
    )


def _npm_tree(tmp: Path) -> Path:
    """A TUI-shaped project: two production packages (one nested), a dev tool, a workspace link and its target."""
    tui = tmp / "tui"
    lock = {
        "name": "@k3code/tui",
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "@k3code/tui", "dependencies": {"left-pad": "1.3.0", "@scope/ink": "file:./packages/ink"}},
            "node_modules/left-pad": {"version": "1.3.0", "license": "WTFPL"},
            "node_modules/react": {"version": "19.2.7", "license": "MIT"},
            "node_modules/react/node_modules/scheduler": {"version": "0.27.0", "license": {"type": "MIT"}},
            "node_modules/typescript": {"version": "6.0.3", "license": "Apache-2.0", "dev": True},
            "node_modules/@scope/ink": {"resolved": "packages/ink", "link": True},
            "packages/ink": {"version": "0.0.1"},
        },
    }
    (tui / "node_modules" / "react" / "node_modules" / "scheduler").mkdir(parents=True)
    (tui / "node_modules" / "left-pad").mkdir()
    (tui / "node_modules" / "react" / "LICENSE").write_text(MIT)
    (tui / "node_modules" / "react" / "node_modules" / "scheduler" / "LICENSE.md").write_text(MIT)
    (tui / "package-lock.json").write_text(json.dumps(lock))
    return tui / "package-lock.json"


def _go_tree(tmp: Path) -> tuple[Path, Path]:
    """`go list -deps -json=Module` output of two platforms (concatenated, with repeats), a module cache, a GOROOT."""
    cache = tmp / "gomod"
    for mod, files in {
        "charm.land/bubbletea/v2@v2.0.8": {"LICENSE": MIT},
        "golang.org/x/sys@v0.47.0": {"LICENSE": BSD, "PATENTS": "Additional IP Rights Grant (Patents)"},
        "github.com/ebitengine/purego@v0.10.2": {"LICENSE": "Apache License\nVersion 2.0, January 2004\n..."},
    }.items():
        (cache / mod).mkdir(parents=True)
        for name, text in files.items():
            (cache / mod / name).write_text(text)
    main = {"Module": {"Path": "github.com/Gaurav-Gosain/tuios", "Main": True, "Dir": str(tmp)}}

    def mod(path: str, version: str) -> dict:
        return {"Module": {"Path": path, "Version": version, "Dir": str(cache / f"{path}@{version}")}}

    linux = [{}, mod("charm.land/bubbletea/v2", "v2.0.8"), mod("golang.org/x/sys", "v0.47.0"), main, {}]
    darwin = [mod("golang.org/x/sys", "v0.47.0"), mod("github.com/ebitengine/purego", "v0.10.2"), main]
    out = tmp / "go-modules.json"
    out.write_text("\n".join(json.dumps(x, indent=1) for x in linux + darwin) + "\n")
    goroot = tmp / "goroot"
    goroot.mkdir()
    (goroot / "LICENSE").write_text(BSD)
    (goroot / "PATENTS").write_text("Additional IP Rights Grant (Patents)")
    (goroot / "VERSION").write_text("go1.26.6\ntime 2026-01-01\n")
    return out, goroot


def test_npm_notices_list_production_packages_with_name_version_and_license(tmp_path: Path) -> None:
    comps = tpn.npm_components(_npm_tree(tmp_path))
    assert [(c.name, c.version, c.license) for c in comps] == [
        ("left-pad", "1.3.0", "WTFPL"),
        ("react", "19.2.7", "MIT"),
        ("scheduler", "0.27.0", "MIT"),  # nested under react/node_modules, the old {"type": ...} form
    ]  # typescript (dev) and the workspace link are left out
    texts = {c.name: c.texts for c in comps}
    assert texts["react"] == [("LICENSE", MIT)] and texts["scheduler"] == [("LICENSE.md", MIT)]
    assert texts["left-pad"] == []  # not installed: the declared license still names it


def test_npm_package_without_a_license_fails(tmp_path: Path) -> None:
    lock = _npm_tree(tmp_path)
    data = json.loads(lock.read_text())
    data["packages"]["node_modules/mystery"] = {"version": "0.1.0"}
    lock.write_text(json.dumps(data))
    with pytest.raises(tpn.NoticeError, match="no license recorded for mystery 0.1.0"):
        tpn.npm_components(lock)


def test_go_notices_cover_every_platform_once_with_the_runtime(tmp_path: Path) -> None:
    mods, goroot = _go_tree(tmp_path)
    comps = tpn.go_components(mods)
    assert [(c.name, c.version, c.license) for c in comps] == [
        ("charm.land/bubbletea/v2", "v2.0.8", "MIT"),
        ("github.com/ebitengine/purego", "v0.10.2", "Apache-2.0"),  # only in the darwin build
        ("golang.org/x/sys", "v0.47.0", "BSD-3-Clause"),  # in both: listed once
    ]
    assert [n for n, _ in comps[2].texts] == ["LICENSE", "PATENTS"]
    runtime = tpn.go_runtime(goroot)
    assert (runtime.version, runtime.license) == ("go1.26.6", "BSD-3-Clause")


def test_go_module_without_a_license_file_or_directory_fails(tmp_path: Path) -> None:
    mods, _ = _go_tree(tmp_path)
    (tmp_path / "gomod" / "golang.org/x/sys@v0.47.0" / "LICENSE").unlink()  # PATENTS alone is no license
    extra = {"Module": {"Path": "example.com/gone", "Version": "v1.0.0"}}  # not downloaded
    mods.write_text(mods.read_text() + json.dumps(extra))
    with pytest.raises(tpn.NoticeError) as e:
        tpn.go_components(mods)
    assert "golang.org/x/sys v0.47.0: no license file" in str(e.value)
    assert "example.com/gone v1.0.0: no module directory" in str(e.value)


def test_generate_writes_one_file_with_own_npm_and_go_notices(tmp_path: Path) -> None:
    lock = _npm_tree(tmp_path)
    mods, goroot = _go_tree(tmp_path)
    out = tmp_path / "THIRD_PARTY_NOTICES.txt"
    args = ["generate", "--own", str(REPO), "--npm-lock", str(lock), "--go-modules", str(mods)]
    r = _run(*args, "--goroot", str(goroot), "-o", str(out))
    assert r.returncode == 0, r.stderr
    assert "(3 npm packages, 3 Go modules)" in r.stdout
    text = out.read_text()
    assert (REPO / "NOTICE").read_text().strip() in text  # k3code's own NOTICE, verbatim
    for p in (REPO / "LICENSES").iterdir():
        assert f"LICENSES/{p.name}" in text and p.read_text().strip() in text
    assert "npm packages in the TUI bundle (production dependencies in tui/package-lock.json)" in text
    summary = [line.split() for line in text.splitlines() if line.startswith("  ")]
    assert ["left-pad", "1.3.0", "WTFPL"] in summary and "typescript" not in text
    assert ["Go", "standard", "library", "and", "runtime", "go1.26.6", "BSD-3-Clause"] in summary
    assert "github.com/ebitengine/purego" in text and "Gaurav-Gosain/tuios" not in text  # k3 itself is k3code's
    assert "Additional IP Rights Grant (Patents)" in text


def test_generate_fails_and_writes_nothing_on_a_missing_license(tmp_path: Path) -> None:
    mods, _ = _go_tree(tmp_path)
    (tmp_path / "gomod" / "charm.land/bubbletea/v2@v2.0.8" / "LICENSE").unlink()
    out = tmp_path / "out.txt"
    r = _run("generate", "--go-modules", str(mods), "-o", str(out))
    assert r.returncode == 1 and "charm.land/bubbletea/v2 v2.0.8: no license file" in r.stderr
    assert not out.exists()


def _wheel(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("k3code/__init__.py", b"")
        for name, data in files.items():
            z.writestr(name, data)
    return path


def test_check_wheel_fails_on_a_wheel_without_the_license_files(tmp_path: Path) -> None:
    bare = _wheel(tmp_path / "k3code-0.1.0-py3-none-any.whl", {})
    r = _run("check-wheel", str(bare))
    assert r.returncode == 1
    for missing in ("k3code/LICENSE is missing", "k3code/NOTICE is missing", "k3code/LICENSES/ is missing"):
        assert missing in r.stderr
    own = {f"k3code/{n}": (REPO / n).read_bytes() for n in ("LICENSE", "NOTICE")}
    own |= {f"k3code/LICENSES/{p.name}": p.read_bytes() for p in (REPO / "LICENSES").iterdir()}
    full = _wheel(tmp_path / "full.whl", own)
    assert _run("check-wheel", "--repo", str(REPO), str(full)).returncode == 0
    stale = _wheel(tmp_path / "stale.whl", own | {"k3code/NOTICE": b"an old NOTICE"})
    r = _run("check-wheel", "--repo", str(REPO), str(stale))
    assert r.returncode == 1 and "k3code/NOTICE differs from the repository's" in r.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build the wheel")
def test_the_release_wheel_ships_license_notice_and_licenses(tmp_path: Path) -> None:
    """`uv build --wheel` in core/, as release.sh runs it: the wheel used to package only src/k3code."""
    out = tmp_path / "wheel"
    r = subprocess.run(
        ["uv", "build", "--quiet", "--wheel", "--out-dir", str(out)],
        cwd=REPO / "core",
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert r.returncode == 0, r.stderr
    (wheel,) = out.glob("*.whl")
    assert tpn.check_wheel(wheel, REPO) == []
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
    assert "k3code/__init__.py" in names and not any(n.endswith("hatch_build.py") for n in names)
