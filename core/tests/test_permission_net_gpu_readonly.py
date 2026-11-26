"""The builtin read-only allowlist covers the listing forms of ``ip``, ``ss``, ``nvidia-smi`` and ``lsblk``. Their
other forms change the network, the GPU or a file (``ip a a``, ``ss -K``, ``nvidia-smi -pl``), read a command file
(``ip -b``, ``ss -F``), switch namespaces (``ip -n``, ``ip netns exec``) or never end (``ip monitor``, ``ss -E``,
``nvidia-smi dmon`` without ``-c``): those still ask."""

from __future__ import annotations

from pathlib import Path

import pytest

from k3code.permissions import PermissionMode, decide

MODES = [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS]

ALLOWED = [
    "ip a",
    "ip -br a",
    "ip -brief address",
    "ip addr show dev eth0",
    "ip link show eth0",
    "ip -4 -s route list",
    "ip route",
    "ip -j link",
    "ip -j -p neigh show",
    "ip rule",
    "ip a | grep inet",
    "ss -tlnp",
    "ss -s",
    "ss -tan state established",
    "ss --tcp --listening --numeric",
    "nvidia-smi",
    "nvidia-smi -L",
    "nvidia-smi -q -d MEMORY -i 0",
    "nvidia-smi --query-gpu=name,memory.used --format=csv",
    "nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader",
    "nvidia-smi topo -m",
    "nvidia-smi dmon -c 1",
    "nvidia-smi pmon -s um -c 3",
    "lsblk",
    "lsblk -f",
    "lsblk -o NAME,SIZE",
    "lsblk -J -a -p -d",
]

NOT_ALLOWED = [
    # ip: a verb other than show (prefix-matched: `a a` = add, `link s` = set), batch files, namespaces, monitor
    "ip a a 1.2.3.4/24 dev eth0",
    "ip link s eth0 down",
    "ip link set eth0 down",
    "ip route add default via 1.2.3.4",
    "ip a flush dev eth0",
    "ip -b f",
    "ip -batch f",
    "ip -force -b f",
    "ip netns exec x sh",
    "ip -n x a",
    "ip -netns x a",
    "ip monitor",
    "ip xfrm state",
    "ip a show dev $X",
    "ip",
    "ip rule f",
    "ip rule flush",
    "ip a eth0",
    "ip a dev eth0",
    "ip a show -b",
    "ip a -br",
    # ss: kill, diag dump, filter file, events, net namespace, an abbreviated long option
    "ss -K dst 1.2.3.4",
    "ss -tK",
    "ss --kill",
    "ss --kil",
    "ss -D f",
    "ss -F f",
    "ss -E",
    "ss -N x",
    "ss dst 1.2.3.4",
    "ss -- -K",
    # nvidia-smi: settings, reset, files, loops, other subcommands
    "nvidia-smi -pl 200",
    "nvidia-smi -c 1",
    "nvidia-smi -pm 1",
    "nvidia-smi -r",
    "nvidia-smi -f out.txt",
    "nvidia-smi -l 1",
    "nvidia-smi -lms 100",
    "nvidia-smi -i 0 -pl 200",
    "nvidia-smi dmon",
    "nvidia-smi dmon -c 1 -f out.txt",
    "nvidia-smi pmon",
    "nvidia-smi mig -cgi 9",
    "nvidia-smi topo",
    "nvidia-smi --query-gpu=$X",
    "nvidia-smi -i -r",
    "nvidia-smi -i {0,-r}",
    "nvidia-smi -i",
    "nvidia-smi dmon -c 0",
    "nvidia-smi dmon -c 1 -c x",
    "nvidia-smi --query-gpu name --format=csv",
    # lsblk: options not listed
    "lsblk --sysroot x",
    # other commands and chains
    "env ip link set eth0 down",
    "X=1 ip link set eth0 down",
    "command ip link set eth0 down",
    "sudo nvidia-smi -r",
    "/usr/sbin/ip link set eth0 down",
    "timeout 5 ss -K",
    "ip a; rm -rf x",
    "iptables -F",
    "ssh h ls",
    "ipx",
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("cmd", ALLOWED)
def test_read_only_net_and_gpu_listings_run_unasked(tmp_path: Path, cmd: str, mode: PermissionMode) -> None:
    assert decide(mode=mode, tool="bash", args={"command": cmd}, cwd=tmp_path).action == "allow", cmd


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("cmd", NOT_ALLOWED)
def test_net_and_gpu_forms_that_change_or_hang_are_not_allowed(tmp_path: Path, cmd: str, mode: PermissionMode) -> None:
    assert decide(mode=mode, tool="bash", args={"command": cmd}, cwd=tmp_path).action != "allow", cmd
