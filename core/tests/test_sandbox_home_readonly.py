"""``sandbox.home_readonly``: ``$HOME`` entries that stay visible, read-only, inside the bash sandbox.

The sandbox hides ``$HOME`` behind an empty tmpfs, so a marker file such as ``~/.myapp/bootstrapped.json`` was
invisible to a model that checks for it, and the model asked its first-run question in every session.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from k3code import paths
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


def test_read_only_home_view_has_no_network(tmp_path):
    home = _home(tmp_path)
    assert "--unshare-net" in sandbox.build_argv(home, home=home, bwrap=BWRAP, readonly_home=True)
    assert "--unshare-net" not in sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP)


def test_read_only_home_view_hides_every_secret_dir_the_engine_refuses():
    from k3code.permissions import engine

    hidden = {*sandbox.HOME_SECRETS, *sandbox.HOME_READONLY_REFUSED, *sandbox.HOME_RO_HIDDEN}
    assert set(engine._HOME_SECRETS) <= hidden


def test_read_only_home_view_hides_top_level_dotenv_and_symlink_targets(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    (home / ".env.local").write_text("K=V\n")
    (home / "dot").mkdir()
    (home / "dot" / "hist").write_text("x\n")
    (home / ".bash_history").symlink_to("dot/hist")
    argv = sandbox.build_argv(home, home=home, bwrap=BWRAP, readonly_home=True)
    masked = [argv[i + 2] for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == "/dev/null"]
    assert str(home / ".env.local") in masked and str(home / "dot" / "hist") in masked
    assert str(home / ".bash_history") not in masked  # a bind onto the link would follow it anyway


def _uv_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    uv = home / ".local" / "share" / "uv"
    for sub in ("python", "tools", "credentials"):
        (uv / sub).mkdir(parents=True)
    (uv / "credentials" / "credentials.toml").write_text('token = "fake-uv-token"\n')
    return home


def test_uv_credentials_are_never_bound_into_the_sandbox(tmp_path):
    """`uv auth dir` is ~/.local/share/uv/credentials (index tokens): only uv's interpreters and tools are bound."""
    home = _uv_home(tmp_path)
    uv = home / ".local" / "share" / "uv"
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    binds = _binds(argv)
    assert str(uv / "python") in binds and str(uv / "tools") in binds
    assert str(uv) not in binds and str(uv / "credentials") not in binds
    assert not any(str(uv / "credentials") in a for a in argv)


def test_uv_dirs_are_bound_only_when_they_exist(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "share" / "uv" / "tools").mkdir(parents=True)
    binds = _binds(sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[]))
    assert str(home / ".local/share/uv/tools") in binds
    assert str(home / ".local/share/uv/python") not in binds


@pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
async def test_uv_credentials_are_unreadable_inside_the_real_sandbox(tmp_path):
    home = _uv_home(tmp_path)
    proj = _proj(tmp_path)
    creds = home / ".local" / "share" / "uv" / "credentials" / "credentials.toml"
    argv = sandbox.build_argv(proj, home=home, home_readonly=[])
    res = await tool_bash({"command": f"cat {creds}; ls {home}/.local/share/uv/python"}, cwd=proj, sandbox=argv)
    assert "fake-uv-token" not in res["stdout"]
    assert "No such file" in res["stderr"]
    assert res["stderr"].count("No such file") == 1  # python/ is still there


# ── ~/.cache is not shared with processes outside the sandbox (K3NOX-639) ──


def _cache_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".cache" / "huggingface").mkdir(parents=True)
    (home / ".cache" / "huggingface" / "token").write_text("fake-hf-token")
    (home / ".config" / "uv").mkdir(parents=True)
    (home / ".config" / "uv" / "uv.toml").write_text('index-url = "https://user:fake-index-pass@example.invalid/"\n')
    return home


def test_the_host_cache_is_never_bound_writable(tmp_path):
    """uv hard-links its cache into environments that run outside the sandbox, so a write in place to a cached file
    from inside would change code that runs unsandboxed. The sandbox gets a cache of its own at the same path."""
    home = _cache_home(tmp_path)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    binds = [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--bind"]
    mine = [src for src, dst in binds if dst == str(home / ".cache")]
    assert len(mine) == 1 and mine[0] != str(home / ".cache")
    assert Path(mine[0]).is_dir() and Path(mine[0]).is_relative_to(paths.home())
    assert str(home / ".cache") not in [src for src, _ in binds]
    assert str(home / ".cache") not in _binds(argv)


def test_the_sandbox_cache_is_bound_even_when_the_host_has_no_cache_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    assert [a for i, a in enumerate(argv) if a == str(home / ".cache") and argv[i - 2] == "--bind"]


def test_the_sandbox_cache_is_private_to_the_user(tmp_path):
    home = _cache_home(tmp_path)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    src = next(argv[i + 1] for i, a in enumerate(argv) if a == "--bind" and argv[i + 2] == str(home / ".cache"))
    assert (Path(src).stat().st_mode & 0o777) == 0o700


def test_an_unusable_sandbox_cache_is_skipped_not_raised(tmp_path, monkeypatch):
    """A sandbox that cannot be built would leave bash running without one: no cache dir means an empty one."""
    home = _cache_home(tmp_path)

    def boom(path):
        raise OSError("read-only file system")

    monkeypatch.setattr(paths, "ensure_private_dir", boom)
    argv = sandbox.build_argv(_proj(tmp_path), home=home, bwrap=BWRAP, home_readonly=[])
    assert not [a for i, a in enumerate(argv) if a == str(home / ".cache") and argv[i - 2] == "--bind"]
    assert not any(a == "--bind" and argv[i + 1] == str(home / ".cache") for i, a in enumerate(argv))


def test_read_only_home_view_hides_the_huggingface_token_and_uv_config(tmp_path):
    home = _cache_home(tmp_path)
    argv = sandbox.build_argv(home, home=home, bwrap=BWRAP, readonly_home=True)
    tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert str(home / ".cache" / "huggingface") in tmpfs and str(home / ".config" / "uv") in tmpfs


def _hardlinked_cache(tmp_path: Path) -> tuple[Path, Path]:
    """A host cache file hard-linked into a 'venv' file, the way uv installs from ~/.cache/uv/archive-v0."""
    home = _cache_home(tmp_path)
    cached = home / ".cache" / "uv" / "archive-v0" / "abc" / "k3probe" / "__init__.py"
    cached.parent.mkdir(parents=True)
    cached.write_text("VALUE = 'original'\n")
    venv_file = home / "outside-venv" / "k3probe" / "__init__.py"
    venv_file.parent.mkdir(parents=True)
    os.link(cached, venv_file)
    assert cached.stat().st_nlink == 2
    return home, cached


needs_bwrap = pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")


@needs_bwrap
async def test_a_hard_linked_cache_file_cannot_be_changed_from_inside_the_sandbox(tmp_path):
    home, cached = _hardlinked_cache(tmp_path)
    venv_file = home / "outside-venv" / "k3probe" / "__init__.py"
    proj = _proj(tmp_path)
    argv = sandbox.build_argv(proj, home=home, home_readonly=[])
    cmd = f"printf \"VALUE = 'changed-in-sandbox'\" > {cached}; echo ok > {home}/.cache/mine; ls {home}/.cache"
    res = await tool_bash({"command": cmd}, cwd=proj, sandbox=argv)
    assert "mine" in res["stdout"]  # a build can still write a cache
    assert venv_file.read_text() == "VALUE = 'original'\n"  # the file outside the sandbox did not change
    assert cached.read_text() == "VALUE = 'original'\n"
    assert not (home / ".cache" / "mine").exists()


@needs_bwrap
async def test_the_huggingface_token_is_unreadable_inside_the_sandbox(tmp_path):
    home = _cache_home(tmp_path)
    proj = _proj(tmp_path)
    for readonly_home in (False, True):
        cwd = home if readonly_home else proj
        argv = sandbox.build_argv(cwd, home=home, home_readonly=[], readonly_home=readonly_home)
        res = await tool_bash(
            {"command": f"cat {home}/.cache/huggingface/token {home}/.config/uv/uv.toml"}, cwd=cwd, sandbox=argv
        )
        assert "fake-hf-token" not in res["stdout"] and "fake-index-pass" not in res["stdout"]
        assert "fake-hf-token" not in res["stderr"] and "fake-index-pass" not in res["stderr"]


def test_probing_bwrap_creates_nothing_under_the_k3code_home(tmp_path, monkeypatch):
    """`usable()` runs at import time of a test module (skipif), before any fixture has moved K3CODE_HOME."""
    k3home = tmp_path / "k3home"
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    monkeypatch.setattr(sandbox, "_probe", None)
    sandbox.usable()
    sandbox.hidden_name_lookup_files()
    assert not k3home.exists()
