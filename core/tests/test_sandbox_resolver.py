"""Name resolution inside the bash sandbox when a resolver file is a link out of the bound system dirs (#137).

The sandbox binds /usr, /etc, /run/systemd/resolve and /run/NetworkManager. On WSL 2 ``/etc/resolv.conf`` is a link
to ``/mnt/wsl/resolv.conf`` (Debian resolvconf: ``../run/resolvconf/resolv.conf``), so every sandboxed command had a
network but could not resolve a single name, and ``/doctor`` still said OK.

The host's /etc cannot be changed here, so the tests point :data:`sandbox.NAME_LOOKUP_FILES` at links in ``tmp_path``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from k3code.reliability import sandbox
from k3code.tools import tool_bash

BWRAP = "/usr/bin/bwrap"
REAL_BWRAP = pytest.mark.skipif(not (shutil.which("bwrap") and sandbox.usable()), reason="bwrap unavailable here")
NAMESERVER = "nameserver 192.0.2.53\n"


def _ops(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


def _wsl(tmp_path: Path) -> tuple[Path, Path]:
    """``etc/resolv.conf`` -> ``<tmp>/mnt/wsl/resolv.conf``, as WSL 2 writes it."""
    target = tmp_path / "mnt" / "wsl" / "resolv.conf"
    target.parent.mkdir(parents=True)
    target.write_text(NAMESERVER)
    link = tmp_path / "etc" / "resolv.conf"
    link.parent.mkdir()
    link.symlink_to(target)
    return link, target


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    return home, proj


@REAL_BWRAP
async def test_a_resolver_link_out_of_the_binds_is_readable_in_the_real_sandbox(tmp_path, monkeypatch):
    """The reported bug: the link is visible inside the sandbox, the file it points to is not."""
    home, proj = _dirs(tmp_path)
    target = tmp_path / "mnt" / "wsl" / "resolv.conf"
    target.parent.mkdir(parents=True)
    target.write_text(NAMESERVER)
    link = proj / "resolv.conf"  # in the project, so the link itself is visible with or without the fix, like /etc
    link.symlink_to(target)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),), raising=False)
    argv = sandbox.build_argv(proj, home=home, home_readonly=[])
    out = await tool_bash({"command": f"cat {link}; echo x > {target}"}, cwd=proj, sandbox=argv)
    assert out["stdout"] == NAMESERVER
    assert "Read-only file system" in out["stderr"]  # bound read-only
    assert target.read_text() == NAMESERVER


@REAL_BWRAP
async def test_a_chain_through_a_linked_directory_resolves_in_the_real_sandbox(tmp_path, monkeypatch):
    """``/etc/resolv.conf -> /var/run/resolvconf/resolv.conf`` with ``/var/run -> /run``: the directory link has to
    exist in the sandbox too, not only the file at the end."""
    home, proj = _dirs(tmp_path)
    real = tmp_path / "run" / "resolvconf" / "resolv.conf"
    real.parent.mkdir(parents=True)
    real.write_text(NAMESERVER)
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "run").symlink_to("../run")
    link = proj / "resolv.conf"
    link.symlink_to(tmp_path / "var" / "run" / "resolvconf" / "resolv.conf")
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    argv = sandbox.build_argv(proj, home=home, home_readonly=[])
    out = await tool_bash({"command": f"cat {link}"}, cwd=proj, sandbox=argv)
    assert out["stdout"] == NAMESERVER, out["stderr"]


def test_the_target_is_bound_read_only_between_the_tmp_and_the_home_tmpfs(tmp_path, monkeypatch):
    home, proj = _dirs(tmp_path)
    link, target = _wsl(tmp_path)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    argv = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    at = [i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1 : i + 3] == [str(target)] * 2]
    assert len(at) == 1
    assert argv.index("/tmp") < at[0] < argv.index(str(home))  # not hidden by the /tmp tmpfs; before $HOME is masked
    assert (str(target), str(link)) in _ops(argv, "--symlink")  # (stands in for /etc/resolv.conf, outside /etc here)


def test_a_relative_chain_recreates_each_link_and_binds_the_real_file(tmp_path, monkeypatch):
    home, proj = _dirs(tmp_path)
    real = tmp_path / "run" / "resolvconf" / "resolv.conf"
    real.parent.mkdir(parents=True)
    real.write_text(NAMESERVER)
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "run").symlink_to("../run")
    (tmp_path / "etc").mkdir()
    link = tmp_path / "etc" / "resolv.conf"
    link.symlink_to("../var/run/resolvconf/resolv.conf")
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    argv = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    assert _ops(argv, "--symlink")[-2:] == [
        ("../var/run/resolvconf/resolv.conf", str(link)),
        ("../run", str(tmp_path / "var" / "run")),
    ]
    assert (str(real), str(real)) in _ops(argv, "--ro-bind")


def test_a_target_the_sandbox_already_shows_adds_nothing(tmp_path, monkeypatch):
    """/etc/resolv.conf -> ../run/systemd/resolve/stub-resolv.conf (systemd-resolved) is visible already."""
    home, proj = _dirs(tmp_path)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", ())
    base = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    link = tmp_path / "resolv.conf"
    link.symlink_to("/etc/passwd")  # a file under /etc, which is bound whole
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    argv = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    assert _ops(argv, "--ro-bind") == _ops(base, "--ro-bind")
    assert ("/etc/passwd", str(link)) in _ops(argv, "--symlink")


def test_a_plain_file_adds_nothing(tmp_path, monkeypatch):
    home, proj = _dirs(tmp_path)
    plain = tmp_path / "resolv.conf"
    plain.write_text(NAMESERVER)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", ())
    base = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(plain),))
    assert sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]) == base


@pytest.mark.parametrize("where", ["home", "home_secret", "k3code_home"])
def test_a_link_into_home_or_the_k3code_home_is_not_exposed(tmp_path, monkeypatch, where):
    """$HOME stays hidden: a resolver link cannot bring a file from it (or from the k3code home) into the sandbox."""
    home, proj = _dirs(tmp_path)
    k3home = tmp_path / "k3home"
    monkeypatch.setenv("K3CODE_HOME", str(k3home))
    target = {
        "home": home / "resolv.conf",
        "home_secret": home / ".ssh" / "id_ed25519",
        "k3code_home": k3home / "env",
    }[where]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret")
    link = tmp_path / "resolv.conf"
    link.symlink_to(target)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", ())
    base = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    assert sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]) == base


def test_a_link_through_a_link_above_home_is_not_recreated(tmp_path, monkeypatch):
    """A link at a directory above $HOME (``/home -> var/home``) would change where the home tmpfs lands."""
    real_homes = tmp_path / "var" / "home"
    (real_homes / "other").mkdir(parents=True)
    (real_homes / "other" / "resolv.conf").write_text(NAMESERVER)
    (tmp_path / "homes").symlink_to(real_homes)
    home = tmp_path / "homes" / "me"
    (real_homes / "me").mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    link = tmp_path / "resolv.conf"
    link.symlink_to(tmp_path / "homes" / "other" / "resolv.conf")
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", ())
    base = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    assert sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]) == base


def test_a_link_into_the_real_home_behind_a_linked_dir_is_not_exposed(tmp_path, monkeypatch):
    """$HOME given as /home/me with /home -> var/home: a resolver link to /var/home/me/... is still $HOME."""
    (tmp_path / "var" / "home" / "me").mkdir(parents=True)
    (tmp_path / "homes").symlink_to(tmp_path / "var" / "home")
    home = tmp_path / "homes" / "me"
    secret = tmp_path / "var" / "home" / "me" / "resolv.conf"
    secret.write_text("secret")
    proj = tmp_path / "proj"
    proj.mkdir()
    link = tmp_path / "resolv.conf"
    link.symlink_to(secret)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", ())
    base = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    assert sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]) == base


@pytest.mark.parametrize("through_link", [False, True])
def test_the_xdg_k3code_home_is_neither_exposed_nor_shadowed(tmp_path, monkeypatch, through_link):
    """$XDG_CONFIG_HOME/k3code is masked by its own tmpfs later; a link recreated above it (XDG_CONFIG_HOME reached
    through a link the resolver chain also crosses) would make that mount, and so the whole sandbox, fail."""
    home, proj = _dirs(tmp_path)
    real_cfg = tmp_path / "cfg"
    (real_cfg / "k3code").mkdir(parents=True)
    (real_cfg / "k3code" / "env").write_text("KEY=secret")
    (real_cfg / "other").mkdir()
    (real_cfg / "other" / "resolv.conf").write_text(NAMESERVER)
    if through_link:
        (tmp_path / "cfglink").symlink_to(real_cfg)
        xdg, target = tmp_path / "cfglink", tmp_path / "cfglink" / "other" / "resolv.conf"
    else:
        xdg, target = real_cfg, real_cfg / "k3code" / "env"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    link = tmp_path / "resolv.conf"
    link.symlink_to(target)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", ())
    base = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    assert sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]) == base


def test_a_link_above_a_dir_the_sandbox_mounts_is_not_recreated(tmp_path, monkeypatch):
    """bwrap cannot put a link where it already made a directory for a mount (``/run`` above /run/systemd/resolve):
    the whole sandbox would fail to start."""
    real = tmp_path / "real"
    (real / "systemd").mkdir(parents=True)
    (real / "resolv.conf").write_text(NAMESERVER)
    (tmp_path / "run").symlink_to(real)
    link = tmp_path / "resolv.conf"
    link.symlink_to(tmp_path / "run" / "resolv.conf")
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    assert sandbox._name_lookup_argv([str(tmp_path / "run" / "systemd")], []) == []
    assert sandbox._name_lookup_argv([], []) != []  # the same chain is fine with nothing mounted below the link


@pytest.mark.parametrize("kind", ["dangling", "directory", "loop"])
def test_a_link_that_does_not_end_at_a_file_adds_nothing_and_never_raises(tmp_path, monkeypatch, kind):
    home, proj = _dirs(tmp_path)
    link = tmp_path / "resolv.conf"
    if kind == "dangling":
        link.symlink_to(tmp_path / "nope")
    elif kind == "directory":
        (tmp_path / "d").mkdir()
        link.symlink_to(tmp_path / "d")
    else:
        link.symlink_to(tmp_path / "loop")
        (tmp_path / "loop").symlink_to(link)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", ())
    base = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(link),))
    assert sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[]) == base


def test_two_resolver_files_sharing_a_link_recreate_it_once(tmp_path, monkeypatch):
    """bwrap fails the whole sandbox on a second --symlink at the same place (older versions)."""
    home, proj = _dirs(tmp_path)
    (tmp_path / "run" / "r").mkdir(parents=True)
    for name in ("resolv.conf", "hosts"):
        (tmp_path / "run" / "r" / name).write_text(name)
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "run").symlink_to("../run")
    links = []
    for name in ("resolv.conf", "hosts"):
        links.append(tmp_path / name)
        links[-1].symlink_to(tmp_path / "var" / "run" / "r" / name)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", tuple(map(str, links)))
    argv = sandbox.build_argv(proj, home=home, bwrap=BWRAP, home_readonly=[])
    dests = [dest for _, dest in _ops(argv, "--symlink")]
    assert dests.count(str(tmp_path / "var" / "run")) == 1


@REAL_BWRAP
def test_hidden_name_lookup_files_names_a_file_the_sandbox_cannot_read(tmp_path, monkeypatch):
    """What /doctor runs: a resolver file readable here but not in the sandbox (here: a link into $HOME)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    hidden = tmp_path / "home" / "resolv.conf"
    hidden.write_text(NAMESERVER)
    _, target = _wsl(tmp_path)
    good = tmp_path / "good"
    good.symlink_to(target)
    bad = tmp_path / "bad"
    bad.symlink_to(hidden)
    monkeypatch.setattr(sandbox, "NAME_LOOKUP_FILES", (str(good), str(bad), str(tmp_path / "absent")))
    assert sandbox.hidden_name_lookup_files() == [str(bad)]


def test_hidden_name_lookup_files_without_bwrap_is_empty(monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: None)
    assert sandbox.hidden_name_lookup_files() == []
