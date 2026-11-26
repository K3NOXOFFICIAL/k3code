"""Builtin read-only commands outside the project: literal paths that hold no secret run without asking; secret paths
ask a person in every mode; broad content walks, expansions and compounds keep asking."""

from __future__ import annotations

import pytest

from k3code.permissions import PermissionMode, decide

ALLOWED_OUTSIDE = [
    "ls /etc",
    "ls -la ~",
    "cat /etc/os-release",
    "head -n5 /var/log/dnf.log",
    "grep -r foo /etc/systemd",
    "find / -name x -maxdepth 2",
    "du -sh ~/Downloads",
    "tail /etc/passwd",
    "find / -name x",
    "sort ~/x",
    "ls /etc | grep ssh",
]

SECRET_READS = [
    "cat /etc/shadow",
    r"cat /etc/sh\adow",
    'cat "/etc/shadow"',
    "cat /et''c/shadow",
    "head ~/.aws/credentials",
    "cat ~/.netrc",
    "grep x ~/.git-credentials",
    "cat /proc/1/environ",
    "tail ~/.bash_history",
    "cat /root/x",
    "cat ~/.kube/config",
    "ls ~/.ssh",
    "cat /etc/ssh/ssh_host_ed25519_key",
    "head ~/.bashrc",
    "cat /etc/sudoers",
    "cat /dev/sda",
]

NOT_ALLOWED = [
    "grep -r x ~",
    "grep -r x /",
    "grep -rn x /etc",
    "rg x ~",
    "grep -r x ~/.config",
    "cat /etc/os-release > ~/.bashrc",
    "cat /etc/hostname; rm -rf x",
    "cat x${IFS}/etc/shadow",
    "cat /etc/{hostname,shadow}",
    "cat $HOME/.ssh/id_rsa",
    "cat /etc/sha*",
    "cat ~root/x",
    "grep -f/etc/shadow x",
    "git log /etc",
    "cat $(echo /etc/hostname)",
]


@pytest.mark.parametrize("command", ALLOWED_OUTSIDE)
@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.AUTO])
def test_read_only_commands_outside_the_project_allow(command: str, mode: PermissionMode, tmp_path) -> None:
    dec = decide(mode=mode, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "allow", (command, dec.message)


@pytest.mark.parametrize("command", SECRET_READS)
@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS, PermissionMode.AUTO])
def test_secret_paths_outside_ask_a_person_in_every_mode(command: str, mode: PermissionMode, tmp_path) -> None:
    dec = decide(mode=mode, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "deny" or (dec.action == "ask" and dec.needs_human), (command, dec.action, dec.message)


@pytest.mark.parametrize("command", NOT_ALLOWED)
def test_broad_walks_expansions_and_compounds_do_not_allow(command: str, tmp_path) -> None:
    dec = decide(mode=PermissionMode.DEFAULT, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action != "allow", command


@pytest.mark.parametrize(
    ("cwd", "command", "allowed"),
    [("/etc", "ls", True), ("/etc", "cat os-release", True), ("/root", "ls", False), ("/", "grep -r x", False)],
)
def test_cwd_outside_the_project(cwd: str, command: str, allowed: bool, tmp_path) -> None:
    dec = decide(mode=PermissionMode.DEFAULT, tool="bash", args={"command": command, "cwd": cwd}, cwd=tmp_path)
    assert (dec.action == "allow") is allowed, (cwd, command, dec.action, dec.message)


#: Secret reads spelled other than a builtin allow pattern: a path or backslash in front of the program, a wrapper, an
#: input redirect, a glob that may match a secret, and the Fedora/RHEL key and VPN folders below /etc.
SECRET_READS_RESPELLED = [
    "/bin/cat /etc/shadow",
    "/usr/bin/head ~/.aws/credentials",
    "env cat /etc/shadow",
    "command cat /etc/shadow",
    r"\cat /etc/shadow",
    "sudo cat /etc/shadow",
    "timeout 5 cat /etc/shadow",
    "nice cat /etc/shadow",
    "cat < /etc/shadow",
    "grep x < ~/.aws/credentials",
    "tr a b < ~/.netrc",
    "cat /etc/sha*",
    "cat ~/.ss*/id_rsa",
    "cat /etc/pki/tls/pri*",
    "cat /*/shadow",
    "grep -r x /etc/pki",
    "cat /etc/pki/tls/private/x.key",
    "cat /etc/wireguard/wg0.conf",
    "ls /etc/letsencrypt/live",
    "ls /etc/pki/libvirt/private",
]


@pytest.mark.parametrize("command", SECRET_READS_RESPELLED)
@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.AUTO])
def test_secret_reads_ask_a_person_however_spelled(command: str, mode: PermissionMode, tmp_path) -> None:
    dec = decide(mode=mode, tool="bash", args={"command": command}, cwd=tmp_path)
    assert dec.action == "deny" or (dec.action == "ask" and dec.needs_human), (command, dec.action, dec.message)


@pytest.mark.parametrize(
    ("command", "default", "auto"),
    [
        ("cat /etc/os-release", "allow", "allow"),
        ("ls /etc", "allow", "allow"),
        ("/bin/cat /etc/os-release", "ask", "allow"),
        ("env cat /etc/hostname", "ask", "allow"),
        ("cat < README.md", "ask", "allow"),
        ("ls /usr/lib/*.so", "ask", "allow"),
    ],
)
def test_harmless_respellings_keep_their_decision(command: str, default: str, auto: str, tmp_path) -> None:
    (tmp_path / "README.md").write_text("x")
    for mode, want in ((PermissionMode.DEFAULT, default), (PermissionMode.AUTO, auto)):
        dec = decide(mode=mode, tool="bash", args={"command": command}, cwd=tmp_path)
        assert (dec.action, dec.needs_human) == (want, False), (command, mode, dec.action, dec.message)
