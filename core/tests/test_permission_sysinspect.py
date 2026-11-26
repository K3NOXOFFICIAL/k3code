"""The read-only process and hardware viewers (``free``, ``uptime``, ``ps``, ``top``, ``lscpu``, ``lsusb``, ``lspci``,
``sensors``, ``hostname``) run unasked in the default mode, but only in the forms that print: no loop (``free -s``,
``top`` without ``-b -n N``), no write (``hostname NAME``, ``sensors -s``), no file (``lspci -F``) and no process
environment (``ps e``, ``ps -o environ``)."""

from __future__ import annotations

import pytest

from k3code.permissions import PermissionMode, decide
from k3code.permissions.engine import _READONLY_OPTS, BUILTIN_BASH_ALLOW

MODES = [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS]

ALLOWED = [
    "free -h",
    "free",
    "free -m -t --wide",
    "uptime",
    "uptime -p",
    "ps aux",
    "ps -ef",
    "ps",
    "ps -eF",
    "ps axo pid,%cpu,args",
    "ps -o pid,user,args -u root",
    "ps --sort=-%mem -A",
    "ps -efly",
    "ps -e -f -w",
    "ps auxww",
    "ps -AfH",
    "top -bn1",
    "top -b -n 1",
    "top -b -n2 -o %CPU -c",
    "top -bn1 -w 200 -p 1 -u root",
    "lscpu",
    "lscpu -e",
    "lscpu -J --extended=cpu,node",
    "lsusb",
    "lsusb -t",
    "lsusb -v -s 1:2",
    "lspci",
    "lspci -nnk",
    "lspci -vv -s 00:02.0",
    "sensors",
    "sensors -A -j",
    "sensors coretemp-isa-0000",
    "hostname",
    "hostname -I",
    "hostname -f",
    "hostname --short",
    "whoami",
    "id root",
    "df",
    "df -h",
    "uname",
    "uname -a",
    "free -h | grep Mem",
    "ps aux | grep python | head -5",
]

NOT_ALLOWED = [
    # chained with a write
    "free -h; rm -rf x",
    "uptime && rm -rf x",
    # free loops
    "free -s 1",
    "free -s1",
    "free --seconds 1",
    "free -c 3",
    "free --count=3",
    "free stray",
    # ps prints every process environment
    "ps auxe",
    "ps e",
    "ps -ef e",
    "ps -auxe",
    "ps -uxe",
    "ps -xe",
    "ps -axe",
    "ps -u root e",
    "ps -o pid,args e",
    "ps --sort pid e",
    "ps -o environ",
    "ps -o pid,environ",
    "ps -O environ",
    "ps O environ",
    "ps --format=pid,environ",
    "ps --format pid,environ",
    "ps -ouser",
    "ps -p1",
    "ps --zz-unlisted",
    "ps ax ?",
    "ps ax *",
    "ps {aux,e}",
    "ps -p -e",
    "ps --",
    # a UNIX line that fails is read again as BSD, where the dashed e prints the environments
    "ps -ef -o pid,args",
    "ps -e -f -o args",
    "ps -ec -o args",
    "ps -eTH",
    "ps -eT --forest",
    "ps -e -q 1",
    "ps -e -u root",
    # top without batch mode, or a value nobody checked
    "top",
    "top -b",
    "top -n1",
    "top -b -n 0",
    "top -b -n 100",
    "top -bn1 -d 0",
    "top -bn1 -s",
    "top -bn1 -p -i",
    "top -bn1 stray",
    # hostname sets the name
    "hostname evil",
    "hostname -s evil",
    "hostname -- evil",
    "hostname -fb",
    "hostname -b",
    "hostname -F MARK",
    "hostname --file MARK",
    "hostname --boot",
    # sensors writes or reads a config
    "sensors -s",
    "sensors --set",
    "sensors -c MARK",
    "sensors --config-file MARK",
    "sensors *",
    "sensors -- -s",
    # lspci / lsusb / lscpu: files, devices, DNS, hardware access
    "lspci -F MARK",
    "lspci -i MARK",
    "lspci -p MARK",
    "lspci -x",
    "lspci -xxx",
    "lspci -q",
    "lspci -Q",
    "lspci -M",
    "lspci -H 1",
    "lspci -A help",
    "lspci -O help",
    "lspci -G",
    "lspci stray",
    "lsusb -D /dev/bus/usb/001/001",
    "lsusb stray",
    "lscpu -s MARK",
    "lscpu --sysroot MARK",
    "lscpu stray",
    "hostname --nis",
    "uptime stray",
    # an unlisted option
    *(f"{p} --zz-unlisted" for p in ("free", "uptime", "top -bn1", "lscpu", "lsusb", "lspci", "sensors", "hostname")),
    # prefix collisions: these are other programs
    "psql -c x",
    "hostnamectl set-hostname x",
    "topgrade",
    "freeze",
]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "MARK").write_text("x\n")
    return tmp_path


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("command", ALLOWED)
def test_inspection_command_allows(command: str, mode: PermissionMode, project) -> None:
    dec = decide(mode=mode, tool="bash", args={"command": command}, cwd=project)
    assert dec.action == "allow", (command, dec.message)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("command", NOT_ALLOWED)
def test_loop_write_or_environment_form_asks(command: str, mode: PermissionMode, project) -> None:
    dec = decide(mode=mode, tool="bash", args={"command": command}, cwd=project)
    assert dec.action != "allow", command


#: Builtin programs without a _READONLY_OPTS entry, on purpose: their patterns take no free argument (``whoami``,
#: ``python --version`` ...), or another check parses them (``git branch``/``git tag``: _ref_list_ok).
SPEC_EXCEPTIONS = {
    "whoami",
    "python",
    "python3",
    "node",
    "go",
    "cargo",
    "git branch",
    "git tag",
}


def test_every_builtin_readonly_program_has_a_spec() -> None:
    """_readonly_argv_ok lets a program without a spec through with any option, so a new builtin needs one."""
    programs = set()
    for pattern in BUILTIN_BASH_ALLOW:
        words = pattern.split()
        programs.add(f"git {words[1]}" if words[0] == "git" else words[0])
    missing = programs - set(_READONLY_OPTS) - SPEC_EXCEPTIONS
    assert not missing, sorted(missing)
    # an exception with a free argument would let any option through
    for pattern in BUILTIN_BASH_ALLOW:
        words = pattern.split()
        name = f"git {words[1]}" if words[0] == "git" else words[0]
        if name in SPEC_EXCEPTIONS - {"git branch", "git tag"}:
            assert "*" not in pattern, pattern
