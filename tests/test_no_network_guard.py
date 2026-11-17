"""The autouse guard in conftest.py must stop every test from leaving this machine."""
import asyncio
import os
import socket

from _socket import socket as PreImportedRawSocket  # bound at collection, before any fixture

import pytest


def test_blocking_connect_to_lan_is_refused():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="reach the network"):
            s.connect(("192.0.2.1", 1502))  # TEST-NET-1, never routed
    finally:
        s.close()


def test_asyncio_open_connection_to_lan_is_refused():
    async def go():
        await asyncio.open_connection("192.0.2.1", 1502)

    with pytest.raises(AssertionError, match="reach the network"):
        asyncio.run(go())


def test_udp_sendto_lan_is_refused():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(AssertionError, match="reach the network"):
            s.sendto(b"x", ("192.0.2.1", 9))
    finally:
        s.close()


def test_loopback_still_allowed():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    c = socket.socket()
    try:
        c.connect(srv.getsockname())
    finally:
        c.close(); srv.close()


def test_raw_c_socket_connect_is_refused():
    import _socket
    s = _socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="reach the network"):
            s.connect(("192.0.2.1", 1502))
        assert s.connect_ex.__name__ == "connect_ex"
        with pytest.raises(AssertionError, match="reach the network"):
            s.connect_ex(("192.0.2.1", 1502))
    finally:
        s.close()


def test_sendmsg_to_lan_is_refused():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(AssertionError, match="reach the network"):
            s.sendmsg([b"x"], [], 0, ("192.0.2.1", 9))
    finally:
        s.close()


def test_dns_resolution_of_foreign_names_is_refused():
    for fn in (lambda: socket.getaddrinfo("inverter.example", 502),
               lambda: socket.gethostbyname("inverter.example")):
        with pytest.raises(AssertionError, match="non-local host name"):
            fn()
    assert socket.getaddrinfo("127.0.0.1", 502)  # numeric stays allowed


def test_asyncio_getaddrinfo_of_foreign_names_is_refused():
    async def go():
        await asyncio.get_running_loop().getaddrinfo("inverter.example", 502)

    with pytest.raises(AssertionError, match="non-local host name"):
        asyncio.run(go())


def test_preimported_raw_socket_alias_is_guarded():
    """An alias bound at import time must not bypass the guard."""
    s = PreImportedRawSocket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="reach the network"):
            s.connect(("192.0.2.1", 1502))
        with pytest.raises(AssertionError, match="reach the network"):
            s.connect_ex(("192.0.2.1", 1502))
    finally:
        s.close()


def test_reverse_dns_is_refused():
    """Reverse DNS lookups must not bypass the network guard."""
    import _socket
    for fn in (
        lambda: socket.gethostbyaddr("192.0.2.1"),
        lambda: socket.getnameinfo(("192.0.2.1", 502), 0),
        lambda: _socket.gethostbyaddr("192.0.2.1"),
        lambda: _socket.getnameinfo(("192.0.2.1", 502), 0),
    ):
        with pytest.raises(AssertionError, match="reach the network|non-local"):
            fn()
    assert socket.getnameinfo(("127.0.0.1", 502), 0)  # loopback stays allowed


def test_any_child_process_is_refused_unless_allowlisted():
    """Shell wrappers / env / interpreters must not slip through."""
    import subprocess
    for argv in (["sh", "-c", "curl http://192.0.2.1/"], ["env", "curl", "192.0.2.1"],
                 ["/usr/bin/env", "python3", "-c", "import socket"], ["python3", "-c", "import socket"]):
        with pytest.raises(AssertionError, match="child process"):
            subprocess.Popen(argv)
    with pytest.raises(AssertionError, match="child process"):
        subprocess.run("echo hi", shell=True)
    with pytest.raises(AssertionError, match="child process"):
        os.system("echo hi")
    with pytest.raises(AssertionError, match="child process"):
        os.posix_spawn("/bin/echo", ["echo"], os.environ)
    with pytest.raises(AssertionError, match="child process"):
        os.execv("/bin/echo", ["echo"])

    async def go():
        await asyncio.create_subprocess_exec("sh", "-c", "true")

    with pytest.raises(AssertionError, match="child process"):
        asyncio.run(go())


def test_spawning_network_tools_is_refused():
    import subprocess
    with pytest.raises(AssertionError, match="child process"):
        subprocess.Popen(["ssh", "192.0.2.1"])
    with pytest.raises(AssertionError, match="child process"):
        subprocess.run("curl http://192.0.2.1/", shell=True)


def test_child_allowlist_is_checked_by_arguments():
    import subprocess
    for argv in (["git", "fetch", "origin"], ["git", "clone", "https://example.invalid/x.git"],
                 ["git", "ls-remote", "git@example.invalid:x/y.git"], ["git", "remote", "update"],
                 ["git", "-c", "safe.directory=*", "-C", "/opt/x", "fetch", "origin", "tag", "v1"],
                 ["python3", "-c", "import socket; socket.create_connection(('192.0.2.1', 1))"],
                 ["python3", "script.py"]):
        with pytest.raises(AssertionError, match="child process"):
            subprocess.Popen(argv)
    out = subprocess.run(["git", "--version"], capture_output=True, text=True)
    assert out.returncode == 0


def test_git_config_and_alias_bypasses_refused(monkeypatch):
    """Git aliases, configured programs and environment overrides must be guarded."""
    import subprocess
    for argv in (
        ["git", "-c", "alias.x=!curl http://192.0.2.1/", "x"],
        ["git", "-c", "core.sshCommand=curl", "--version"],
        ["git", "--config-env=alias.x=FOO", "x"],
        ["git", "--exec-path=/tmp/evil", "--version"],
        ["git", "config", "alias.x", "!curl http://192.0.2.1/"],
        ["git", "config", "core.sshCommand", "curl"],
        ["git", "config", "core.pager", "curl"],
    ):
        with pytest.raises(AssertionError, match="child process"):
            subprocess.Popen(argv)
    with pytest.raises(AssertionError, match="child process"):
        subprocess.Popen(["git", "--version"], env={"GIT_SSH_COMMAND": "curl", "PATH": os.environ["PATH"]})
    monkeypatch.setenv("GIT_SSH_COMMAND", "curl")
    with pytest.raises(AssertionError, match="child process"):
        subprocess.Popen(["git", "--version"])
    monkeypatch.delenv("GIT_SSH_COMMAND")
    with pytest.raises(AssertionError, match="child process"):
        subprocess.Popen("git --version; curl http://192.0.2.1/", shell=True)
    with pytest.raises(AssertionError, match="child process"):
        os.system("git --version; curl http://192.0.2.1/")
    # harmless local use stays possible
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    assert subprocess.run(["git", "-C", ".", "config", "--get", "user.name"],
                          capture_output=True).returncode in (0, 1)


def test_python_dynamic_code_and_executable_refused():
    import sys
    import subprocess
    exe = sys.executable
    for code in (
        "import runpy; runpy.run_path('/tmp/x.py')",
        "import importlib; importlib.import_module('socket')",
        "__import__('socket').create_connection(('192.0.2.1', 1))",
        "exec('import ' + 'sock' + 'et')",
        "eval('1')",
        "import os; os.system('curl x')",
        "import os; os.execv('/bin/sh', ['sh'])",
        "import subprocess",
        "import sys; sys.modules",
        "getattr(__builtins__, 'ex' 'ec')('1')",
        "x = ().__class__",
        "print(1",
    ):
        with pytest.raises(AssertionError, match="child process"):
            subprocess.Popen([exe, "-c", code])
    with pytest.raises(AssertionError, match="child process"):
        subprocess.Popen([exe, "-m", "http.server"])
    with pytest.raises(AssertionError, match="child process"):
        subprocess.Popen(["python3", "-c", "print(1)"], executable="/usr/bin/curl")
    with pytest.raises(AssertionError, match="child process"):
        subprocess.Popen(["git", "--version"], executable="/usr/bin/curl")
    with pytest.raises(AssertionError, match="child process"):
        os.execv("/usr/bin/git", ["git", "fetch", "origin"])
    out = subprocess.run([exe, "-c", "import sys, time\nprint('ok')"], capture_output=True)
    assert out.stdout.strip() == b"ok"
