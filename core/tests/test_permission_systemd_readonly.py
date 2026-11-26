"""The builtin read-only allowlist runs the query forms of ``systemctl`` and ``journalctl`` unasked: status, unit
listings, unit files and logs. Every verb that changes a unit, every option that reaches another system or writes the
journal, and every option an optional-value option might be hiding still asks."""

from __future__ import annotations

import pytest

from k3code.permissions import PermissionMode, decide

MODES = [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS]

ALLOWED = [
    "systemctl",
    "systemctl --user",
    "systemctl status sshd",
    "systemctl --user status k3code",
    "systemctl status sshd --no-pager -l",
    "systemctl -al --no-legend list-units",
    "systemctl --user list-units",
    "systemctl list-units --type=service --state=running --plain",
    "systemctl -t service list-units",
    "systemctl list-unit-files 'ssh*'",
    "systemctl list-timers --all",
    "systemctl list-sockets",
    "systemctl list-dependencies sshd",
    "systemctl list-jobs",
    "systemctl show sshd -p ActiveState --value",
    "systemctl show --property=MainPID sshd",
    "systemctl cat sshd",
    "systemctl -n 20 -o short-iso status sshd",
    "systemctl is-active sshd",
    "systemctl -q is-enabled sshd",
    "systemctl is-failed sshd",
    "systemctl is-system-running",
    "systemctl get-default",
    "systemctl --system -r list-units",
    "systemctl status sshd | grep Active",
    "systemctl status sshd 2>&1 | tail -5",
    "journalctl",
    "journalctl -n 50 --no-pager",
    "journalctl -n",
    "journalctl -n50",
    "journalctl --lines=50",
    "journalctl --user -u k3code -n 20",
    "journalctl -u sshd --since=-1h --until=now",
    "journalctl -S today -U now -p warning",
    "journalctl --user-unit=k3code -o json",
    "journalctl -b",
    "journalctl -b -1 -n 20",
    "journalctl --boot=-1",
    "journalctl -kb",
    "journalctl -xe",
    "journalctl -r -q --no-hostname --utc",
    "journalctl -g error -t sshd --case-sensitive=false",
    "journalctl --grep=fail --identifier=sudo -a",
    "journalctl --list-boots",
    "journalctl --disk-usage",
    "journalctl --system --dmesg --reverse --catalog --pager-end",
    "journalctl -u sshd -n 5 | grep Accepted",
]

NOT_ALLOWED = [
    "systemctl restart sshd",
    "systemctl start sshd",
    "systemctl stop sshd",
    "systemctl reload sshd",
    "systemctl enable sshd",
    "systemctl disable sshd",
    "systemctl mask sshd",
    "systemctl edit sshd",
    "systemctl kill sshd",
    "systemctl --user daemon-reload",
    "systemctl set-environment A=b",
    "systemctl reboot",
    "systemctl help sshd",
    "systemctl -t status restart x",
    "systemctl -at status restart x",
    "systemctl --type status restart x",
    "systemctl -p status stop x",
    "systemctl -- restart x",
    'systemctl "restart" x',
    "systemctl status x --now restart",
    "systemctl -H h status x",
    "systemctl --host=h status x",
    "systemctl -M m status x",
    "systemctl --root=/ status x",
    "systemctl --us status x",
    "systemctl -i status x",
    "systemctlx status",
    "sudo systemctl status x",
    "env systemctl restart x",
    "SYSTEMD_PAGER=sh systemctl status x",
    "journalctl -f",
    "journalctl --follow",
    "journalctl -u sshd -f",
    "journalctl -n --rotate",
    "journalctl -xn --rotate",
    "journalctl --lines --rotate",
    "journalctl -b --vacuum-time=1d",
    "journalctl --boot --flush",
    "journalctl --vacuum-size=1M",
    "journalctl --sync",
    "journalctl --relinquish-var",
    "journalctl --setup-keys",
    "journalctl --update-catalog",
    "journalctl -D /tmp/j",
    "journalctl --file=x.journal",
    "journalctl --root=/",
    "journalctl -M m",
    "journalctl --cursor-file=c",
    "journalctl -n 5 > ~/.bashrc",
    "journalctl -n 5 > out.txt",
    "SYSTEMD_PAGER=sh journalctl -n 5",
]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "MARK").write_text("x\n")
    return tmp_path


def _action(command: str, mode: PermissionMode, cwd) -> str:
    return decide(mode=mode, tool="bash", args={"command": command}, cwd=cwd).action


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("command", ALLOWED)
def test_read_only_systemd_query_allows(command: str, mode: PermissionMode, project) -> None:
    assert _action(command, mode, project) == "allow", command


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("command", NOT_ALLOWED)
def test_systemd_change_or_hidden_option_does_not_allow(command: str, mode: PermissionMode, project) -> None:
    assert _action(command, mode, project) in ("ask", "deny"), command
