"""``sandbox.home_readonly``: ``$HOME`` entries that stay visible, read-only, inside the bash sandbox.

The sandbox hides ``$HOME`` behind an empty tmpfs, so a marker file such as ``~/.myapp/bootstrapped.json`` was
invisible to a model that checks for it, and the model asked its first-run question in every session.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from k3code.reliability import sandbox
from k3code.tools import tool_bash

BWRAP = "/usr/bin/bwrap"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".myapp").mkdir(parents=True)
    (home / ".myapp" / "bootstrapped.json").write_text("{}")
    return home


def _proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return proj


def _binds(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]


def test_listed_entry_is_bound_read_only_after_the_home_tmpfs(tmp_path):
    home = _home(tmp_path)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[".myapp"])
    assert str(home / ".myapp") in _binds(argv)
    assert argv.index("--tmpfs") < argv.index(str(home / ".myapp"))  # a bind below the tmpfs, not hidden by it


def test_nothing_extra_is_bound_by_default(tmp_path):
    home = _home(tmp_path)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    assert str(home / ".myapp") not in argv


@pytest.mark.parametrize(
    "entry",
    [
        "",
        " ",
        ".",
        "..",
        "../x",
        "/etc",
        ".ssh",
        ".ssh/id_ed25519",
        ".config/k3code",
        ".config/k3code/env",
        "a/../.ssh",
    ],
)
def test_unsafe_entries_are_skipped(tmp_path, entry):
    home = _home(tmp_path)
    for secret in (".ssh", ".config/k3code"):
        (home / secret).mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "id_ed25519").write_text("k")
    (home / ".config" / "k3code" / "env").write_text("K=v")
    proj = _proj(tmp_path)
    base = _binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]))
    argv = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[entry])
    assert _binds(argv) == base  # nothing was added


def test_a_parent_of_a_secret_is_skipped(tmp_path):
    home = _home(tmp_path)
    (home / ".config" / "k3code").mkdir(parents=True)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[".config"])
    assert str(home / ".config") not in _binds(argv)


def test_a_symlink_into_a_secret_is_skipped(tmp_path):
    home = _home(tmp_path)
    (home / ".ssh").mkdir()
    (home / "innocent").symlink_to(home / ".ssh")
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=["innocent"])
    assert str(home / "innocent") not in _binds(argv)


def test_a_missing_entry_is_ignored(tmp_path):
    home = _home(tmp_path)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[".nope"])
    assert str(home / ".nope") not in argv


def test_the_user_config_is_the_default_source(tmp_path, monkeypatch):
    k3home = tmp_path / "k3home"
    k3home.mkdir()
    (k3home / "config.yaml").write_text(yaml.safe_dump({"sandbox": {"home_readonly": [".myapp", 7, None]}}))
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    assert sandbox.configured_home_readonly() == [".myapp"]
    home = _home(tmp_path)
    assert str(home / ".myapp") in _binds(sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP))


@pytest.mark.parametrize("section", [None, "x", ["a"], {"home_readonly": ".myapp"}, {"home_readonly": {"a": 1}}])
def test_a_malformed_section_means_no_extra_entries(tmp_path, monkeypatch, section):
    k3home = tmp_path / "k3home"
    k3home.mkdir()
    (k3home / "config.yaml").write_text(yaml.safe_dump({"sandbox": section}))
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    assert sandbox.configured_home_readonly() == []


def test_a_project_config_cannot_widen_the_sandbox(tmp_path, monkeypatch):
    k3home = tmp_path / "k3home"
    k3home.mkdir()
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    project = tmp_path / "proj"
    (project / ".k3code").mkdir(parents=True)
    (project / ".k3code" / "config.yaml").write_text(yaml.safe_dump({"sandbox": {"home_readonly": [".gnupg"]}}))
    monkeypatch.chdir(project)
    assert sandbox.configured_home_readonly() == []


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_a_listed_marker_is_readable_and_read_only_inside_the_real_sandbox(tmp_path):
    home = _home(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    hidden = sandbox.build_argv(proj, home=home, home_readonly=[])
    shown = sandbox.build_argv(proj, home=home, home_readonly=[".myapp"])
    marker = home / ".myapp" / "bootstrapped.json"
    probe = {"command": f"ls {marker}; echo x > {marker}; echo x > {home}/.myapp/new"}
    without = await tool_bash(probe, cwd=proj, sandbox=hidden)
    assert "No such file" in without["stderr"]
    with_it = await tool_bash(probe, cwd=proj, sandbox=shown)
    assert str(marker) in with_it["stdout"]  # `ls` finds it
    assert with_it["stderr"].count("Read-only file system") == 2  # and neither write got through
    assert marker.read_text() == "{}" and not (home / ".myapp" / "new").exists()


def test_an_uninspectable_entry_is_skipped_not_raised(tmp_path):
    home = _home(tmp_path)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[".myapp", "a" * 300])
    assert str(home / ".myapp") in _binds(argv)  # the good entry survives the bad one


def test_an_unreadable_parent_is_skipped_not_raised(tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    home = _home(tmp_path)
    locked = home / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0)
    try:
        argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=["locked/inner", ".myapp"])
    finally:
        locked.chmod(0o700)
    assert str(home / "locked" / "inner") not in _binds(argv)
    assert str(home / ".myapp") in _binds(argv)


def test_non_string_entries_are_skipped(tmp_path):
    home = _home(tmp_path)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[None, 7, [".myapp"], ".myapp"])
    assert _binds(argv).count(str(home / ".myapp")) == 1


@pytest.mark.parametrize("entry", [".gnupg", ".aws", ".gnupg/private-keys-v1.d", ".k3code", ".k3code/config.yaml"])
def test_other_secret_folders_are_refused(tmp_path, monkeypatch, entry):
    home = _home(tmp_path)
    monkeypatch.setenv("K3CODE_HOME", str(home / ".k3code"))
    for d in (".gnupg/private-keys-v1.d", ".aws", ".k3code"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / ".k3code" / "config.yaml").write_text("x: 1")
    proj = _proj(tmp_path)
    base = _binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]))
    assert _binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[entry])) == base


def test_an_entry_above_the_project_is_refused(tmp_path):
    home = _home(tmp_path)
    proj = home / "work" / "proj"
    proj.mkdir(parents=True)
    base = _binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]))
    assert _binds(sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=["work"])) == base


def test_the_cache_stays_writable(tmp_path):
    home = _home(tmp_path)
    (home / ".cache").mkdir()
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[".cache"])
    assert str(home / ".cache") not in _binds(argv)


def test_an_entry_inside_the_project_is_read_only_after_the_project_bind(tmp_path):
    home = _home(tmp_path)
    proj = home / "work" / "proj"
    (proj / ".myapp").mkdir(parents=True)
    argv = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=["work/proj/.myapp"])
    bind = [i for i, a in enumerate(argv) if a == "--bind" and argv[i + 1] == str(proj)][0]
    ro = [i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == str(proj / ".myapp")][0]
    assert ro > bind  # a later mount wins: the entry is read-only although its parent is writable
