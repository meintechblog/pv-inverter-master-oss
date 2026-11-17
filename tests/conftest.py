"""Shared fixtures."""
import ast
import asyncio.base_events
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys

import _socket

import pytest

from pv_inverter_master import modbus_gateway
from pv_inverter_master.modbus_gateway import reset_gateways


@pytest.fixture(autouse=True)
def _fresh_modbus_gateways(monkeypatch):
    """Gateways are process-wide per host; give every test its own set (and
    its own asyncio locks, which bind to the test's event loop). The inter-
    transaction gap is 0 here so tests stay fast; the gateway tests set an
    explicit gap and assert it."""
    monkeypatch.setattr(modbus_gateway, "DEFAULT_MIN_GAP_S", 0.0)

    real_client = modbus_gateway.AsyncModbusTcpClient

    def _no_real_client(*args, **kwargs):
        host = args[0] if args else kwargs.get("host")
        if host in ("127.0.0.1", "localhost", "::1"):
            return real_client(*args, **kwargs)  # fake servers on this machine
        raise AssertionError(
            f"test tried to open a REAL Modbus client {args} {kwargs} - "
            "patch pv_inverter_master.modbus_gateway.AsyncModbusTcpClient or use a 127.0.0.1 fake server (tests/fake_modbus_server.py)"
        )

    monkeypatch.setattr(modbus_gateway, "AsyncModbusTcpClient", _no_real_client)
    reset_gateways()
    yield
    reset_gateways()


def _is_loopback(address) -> bool:
    if not isinstance(address, tuple):
        return True  # AF_UNIX paths stay on this machine
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_address(address):
    if not _is_loopback(address):
        raise AssertionError(f"test tried to reach the network: {address!r}")


def _check_name(host):
    """Name resolution: only loopback names/numeric addresses and the wildcard
    bind (None / "") resolve in tests; anything else would hit a real resolver
    (and then a real device). Tests needing a name mock socket.getaddrinfo."""
    if host in (None, "", b"", "localhost", b"localhost"):
        return
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    try:
        ipaddress.ip_address(host)  # numeric: no DNS involved
    except ValueError:
        raise AssertionError(f"test tried to resolve a non-local host name: {host!r}") from None


class _GuardedRawSocket(_socket.socket):
    """Replacement for the C-level ``_socket.socket``.

    Installed at conftest import time - BEFORE the test modules are collected -
    and swapped into every module attribute that still points at the original
    class, so an alias bound by ``from _socket import socket`` is guarded too.
    Only ever stricter: it stays installed for the session.
    """

    def connect(self, address):
        _check_address(address)
        return super().connect(address)

    def connect_ex(self, address):
        _check_address(address)
        return super().connect_ex(address)

    def sendto(self, data, *args):
        _check_address(args[-1])
        return super().sendto(data, *args)

    def sendmsg(self, buffers, ancdata=(), flags=0, address=None):
        if address is not None:
            _check_address(address)
            return super().sendmsg(buffers, ancdata, flags, address)
        return super().sendmsg(buffers, ancdata, flags)


def _install_raw_socket_guard() -> None:
    original = _socket.socket
    if original is _GuardedRawSocket:
        return
    _socket.socket = _GuardedRawSocket
    for mod in list(sys.modules.values()):
        d = getattr(mod, "__dict__", None)
        if not isinstance(d, dict):
            continue
        for name, value in list(d.items()):
            if value is original and mod is not _socket:
                try:
                    d[name] = _GuardedRawSocket
                except Exception:  # pragma: no cover - read-only module dicts
                    pass


_install_raw_socket_guard()


#: Child programs tests really need, each with a check that keeps it off the
#: network (see :func:`_check_child`). Anything else is refused.
ALLOWED_CHILD_PROGRAMS = frozenset({"git", "python", "python3"})

_NET_TOKENS = ("socket", "urllib", "http", "ssl", "connect", "pymodbus", "asyncio",
               "ftplib", "smtplib", "telnet", "requests", "aiohttp", "subprocess", "os.system")
_GIT_REMOTE_CMDS = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})
_GIT_FORBIDDEN_CMDS = frozenset({"submodule", "daemon", "send-email", "imap-send"})
_GIT_REMOTE_CONFIG_ONLY = frozenset({"add", "set-url", "get-url", "remove", "rm", "rename"})
_SCP_LIKE = re.compile(r"^[^/\s:]+@[^/\s:]+:|^[A-Za-z0-9.-]+:[^/]")


def _git_remote_is_local(name: str, argv: list[str], cwd) -> bool:
    """Resolve a remote NAME from the repository's own config (local, no network)."""
    cmd = ["git"]
    rest = argv[1:]
    for j, a in enumerate(rest):               # carry -C <dir> over
        if a == "-C" and j + 1 < len(rest):
            cmd += ["-C", rest[j + 1]]
    cmd += ["config", "--get", f"remote.{name}.url"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    url = out.stdout.strip()
    if out.returncode != 0 or not url:
        return False
    if "://" in url:
        return url.startswith("file://")
    return not _SCP_LIKE.match(url) and os.path.exists(url)


#: git global options a test may use. ``-c`` / ``--config-env`` / ``--exec-path``
#: are NOT among them: ``git -c alias.x='!curl ...' x`` or ``core.sshCommand``
#: would run arbitrary programs.
_GIT_VALUE_OPTS = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})
_GIT_FLAG_OPTS = frozenset({"--no-pager", "--no-optional-locks", "--bare", "--literal-pathspecs",
                            "--no-replace-objects", "-P", "--version", "--help"})
#: ``git config`` keys that may be WRITTEN (no alias.*, core.sshCommand, core.pager, ...).
_GIT_SAFE_CONFIG_KEYS = frozenset({"user.email", "user.name", "commit.gpgsign", "tag.gpgsign",
                                   "init.defaultbranch", "uploadpack.allowanysha1inwant", "advice.detachedhead", "gc.auto"})
_GIT_SAFE_ENV = frozenset({"GIT_TERMINAL_PROMPT", "GIT_CONFIG_NOSYSTEM", "GIT_DIR", "GIT_WORK_TREE",
                           "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                           "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
                           "GIT_PAGER_IN_USE", "GIT_OPTIONAL_LOCKS"})


#: Inherited environment variables through which git runs a program / reads config.
_GIT_RUNS_PROGRAMS_ENV = frozenset({"GIT_SSH", "GIT_SSH_COMMAND", "GIT_PROXY_COMMAND",
                                    "GIT_EXEC_PATH", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
                                    "GIT_ASKPASS", "GIT_EXTERNAL_DIFF", "GIT_PAGER",
                                    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"})


def _git_args_local(argv: list[str], cwd=None) -> bool:
    """True when this git command cannot reach a remote host or run a program."""
    rest = list(argv[1:])
    i = 0
    while i < len(rest) and rest[i].startswith("-"):      # global options
        opt = rest[i]
        name = opt.split("=", 1)[0]
        if name in _GIT_VALUE_OPTS:
            i += 1 if "=" in opt else 2
        elif opt in _GIT_FLAG_OPTS:
            i += 1
        else:
            return False        # -c, --config-env, --exec-path, anything unknown
    if i >= len(rest):
        return True
    sub, args = rest[i], rest[i + 1:]
    for a in args:
        if "://" in a and not a.startswith("file://"):
            return False
        if _SCP_LIKE.match(a):
            return False
    if sub in _GIT_FORBIDDEN_CMDS:
        return False
    if sub == "config":
        reading = any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l") for a in args)
        keys = [a for a in args if not a.startswith("-")]
        if not reading and (not keys or keys[0].lower() not in _GIT_SAFE_CONFIG_KEYS):
            return False        # alias.*, core.sshCommand, core.pager, url.*, ...
    if sub == "remote":     # config only; update/prune/show would contact the remote
        return bool(args) and args[0] in _GIT_REMOTE_CONFIG_ONLY
    if sub in _GIT_REMOTE_CMDS:
        positional = [a for a in args if not a.startswith("-")]
        # the repository must be an existing local path / file:// URL, never a
        # remote NAME (origin) that could point anywhere
        if not positional:
            return False
        repo = positional[0]
        if repo.startswith("file://") or os.path.exists(repo):
            return True
        return _git_remote_is_local(repo, argv, cwd)
    return True


#: FULL module paths a ``python -c`` snippet of a test may ``import`` (nothing
#: network-capable, no way to import something else dynamically). Only the
#: exact paths count: ``import a.b.c`` is checked as ``a.b.c``, not as ``a``.
#: The one project module is the instance lock (no network code).
_PY_ALLOWED_IMPORTS = frozenset({"sys", "time", "os", "os.path", "json", "pathlib", "fcntl",
                                 "signal", "textwrap", "pv_inverter_master.instance_lock"})
#: ``from <module> import <names>`` is allowed only for these modules (the names
#: of sys/os/fcntl/signal are not vetted, so no from-import there at all).
_PY_FROM_IMPORT_MODULES = frozenset({"time", "json", "pathlib", "textwrap",
                                     "pv_inverter_master.instance_lock"})
_PY_BANNED_NAMES = frozenset({"exec", "eval", "compile", "__import__", "getattr", "setattr",
                              "delattr", "globals", "locals", "vars", "breakpoint", "input",
                              "importlib", "runpy", "subprocess", "socket", "ctypes"})
_OS_BANNED_PREFIXES = ("system", "popen", "exec", "spawn", "posix_spawn", "fork", "kill",
                       "putenv", "dup", "pipe", "startfile")


def _python_snippet_is_safe(code: str) -> bool:
    """Static check of ``python -c`` code: only allowlisted imports, no dynamic
    code or import machinery (exec/eval/__import__/runpy/importlib/getattr...),
    no dunder attribute access, no process/exec functions of ``os``."""
    if any(tok in code.lower() for tok in _NET_TOKENS):
        return False
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return False
    attr_values = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name not in _PY_ALLOWED_IMPORTS or a.asname for a in node.names):
                return False        # also no alias: ``import os as o`` hides the os checks
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in _PY_FROM_IMPORT_MODULES:
                return False
            if any(a.name == "*" or a.asname or a.name.startswith("_")
                   or a.name in _PY_BANNED_NAMES for a in node.names):
                return False
        elif isinstance(node, ast.Name):
            if node.id in _PY_BANNED_NAMES or node.id.startswith("__"):
                return False
            if node.id in ("os", "sys") and isinstance(node.ctx, ast.Load) \
                    and id(node) not in attr_values:
                return False        # ``o = os`` / ``f(os)``: escapes the attribute checks
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in _PY_BANNED_NAMES:
                return False
            if node.attr.startswith(_OS_BANNED_PREFIXES) or node.attr in ("os", "sys"):
                return False        # any object: ``os.path.os.system`` too
            if isinstance(node.value, ast.Name) and node.value.id == "sys" \
                    and node.attr in ("modules", "meta_path", "path_hooks", "settrace",
                                      "setprofile", "addaudithook"):
                return False        # reaching importlib machinery through sys
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in _PY_BANNED_NAMES or node.value.startswith("__"):
                return False        # e.g. a name smuggled for getattr-style tricks
    return True


def _real(path) -> str:
    return os.path.realpath(path) if path else ""


def _resolves_to(program: str, expected: str, env=None) -> bool:
    """``program`` (as the OS would find it) is really the expected executable."""
    path = None
    if env is not None:
        path = env.get("PATH") if hasattr(env, "get") else None
    found = program if os.path.dirname(program) else shutil.which(program, path=path)
    return bool(found) and _real(found) == _real(expected)


def _is_this_python(program: str, env=None) -> bool:
    """``program`` is the interpreter running the tests (or the PATH python3)."""
    return any(_resolves_to(program, exe, env)
               for exe in (sys.executable, shutil.which("python3") or "", shutil.which("python") or "")
               if exe)


def _check_child(argv0, args, cwd=None, *, env=None, executable=None, shell=False,
                 via_shell=False) -> None:
    """Child processes: none, except a small allowlist that is checked by ARGUMENTS.

    The first word alone is NOT enough to judge a command (``sh -c 'curl ...'``,
    ``env curl ...``, an interpreter running socket code), and an exec'd
    process has none of the Python patches below - so the default is: refuse.
    ``git`` only for local operations (no remote name / URL, no ``-c``, no
    alias/command-running config), ``python`` only for ``-c`` snippets that pass
    a static check. ``shell=True``, ``os.system`` and an ``executable=`` that is
    not the program named in argv[0] are always refused.
    """
    if isinstance(args, (str, bytes, os.PathLike)):
        argv = os.fsdecode(args).split()
    else:
        argv = [os.fsdecode(x) for x in args]
    ok = False
    prog = os.fsdecode(argv0).split()[0] if os.fsdecode(argv0).split() else ""
    name = os.path.basename(prog)
    if shell or via_shell or isinstance(args, (str, bytes)) and len(argv) != 1:
        ok = False
    elif executable is not None and _real(os.fsdecode(executable)) != _real(
            shutil.which(prog) or prog):
        ok = False
    elif name == "git" and argv and os.path.basename(argv[0]) == "git" \
            and _resolves_to(argv[0], shutil.which("git") or "", env):
        # GIT_SSH_COMMAND, GIT_CONFIG_COUNT/KEY/VALUE (= -c), GIT_EXEC_PATH ... run programs
        env_ok = not any(
            k.startswith("GIT_") and k not in _GIT_SAFE_ENV
            for k in (env if env is not None else
                      [k for k in os.environ if k in _GIT_RUNS_PROGRAMS_ENV]))
        ok = env_ok and _git_args_local(argv, cwd)
    elif name in ("python", "python3") and len(argv) >= 3 and argv[1] == "-c" \
            and _is_this_python(argv[0], env):
        ok = _python_snippet_is_safe(argv[2])
    if not ok:
        raise AssertionError(f"test tried to start a child process: {args!r}")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Last line of defence below every client (gateway, scanner, Venus write,
    anomaly detector): no test may reach a non-loopback address. A test
    reached the real SolarEdge inverter because a mock did not apply.

    Covers: socket connect/connect_ex/sendto/sendmsg (also on the C-level
    ``_socket.socket`` class), name resolution (socket.getaddrinfo & co.),
    asyncio loop.getaddrinfo/create_connection/create_datagram_endpoint and
    refusing every child process (allowlist ALLOWED_CHILD_PROGRAMS), reverse DNS."""
    for name in ("connect", "connect_ex"):
        real = getattr(socket.socket, name)

        def make(real=real):
            def guarded(self, address):
                _check_address(address)
                return real(self, address)
            return guarded
        monkeypatch.setattr(socket.socket, name, make())

    real_sendto = socket.socket.sendto

    def sendto(self, data, *args):
        _check_address(args[-1])
        return real_sendto(self, data, *args)

    real_sendmsg = socket.socket.sendmsg

    def sendmsg(self, buffers, ancdata=(), flags=0, address=None):
        if address is not None:
            _check_address(address)
        return real_sendmsg(self, buffers, ancdata, flags, address) if address is not None \
            else real_sendmsg(self, buffers, ancdata, flags)

    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)

    # The C-level ``_socket.socket`` is guarded at import time (see above);
    # fail loudly if something swapped it back.
    assert _socket.socket is _GuardedRawSocket

    # Name resolution.
    real_gai = _socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        _check_name(host)
        return real_gai(host, *args, **kwargs)

    monkeypatch.setattr(_socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    for fn in ("gethostbyname", "gethostbyname_ex"):
        real_fn = getattr(_socket, fn)

        def make_n(real_fn=real_fn):
            def guarded(host):
                _check_name(host)
                return real_fn(host)
            return guarded
        monkeypatch.setattr(_socket, fn, make_n())
        monkeypatch.setattr(socket, fn, make_n())

    # Reverse DNS also reaches a real resolver.
    real_gethostbyaddr = _socket.gethostbyaddr
    real_getnameinfo = _socket.getnameinfo

    def gethostbyaddr(ip):
        _check_name(ip)
        _check_address((ip,))
        return real_gethostbyaddr(ip)

    def getnameinfo(sockaddr, flags):
        _check_address(sockaddr)
        return real_getnameinfo(sockaddr, flags)

    for mod in (_socket, socket):
        monkeypatch.setattr(mod, "gethostbyaddr", gethostbyaddr)
        monkeypatch.setattr(mod, "getnameinfo", getnameinfo)

    # asyncio entry points (resolve + connect on the loop).
    loop_cls = asyncio.base_events.BaseEventLoop
    real_loop_gai = loop_cls.getaddrinfo
    real_create_connection = loop_cls.create_connection
    real_datagram = loop_cls.create_datagram_endpoint

    async def loop_gai(self, host, port, **kwargs):
        _check_name(host)
        return await real_loop_gai(self, host, port, **kwargs)

    async def create_connection(self, protocol_factory, host=None, port=None, **kwargs):
        if host is not None:
            _check_name(host)
            _check_address((host, port))
        return await real_create_connection(self, protocol_factory, host, port, **kwargs)

    async def create_datagram_endpoint(self, protocol_factory, local_addr=None,
                                       remote_addr=None, **kwargs):
        if remote_addr is not None:
            _check_address(remote_addr)
        return await real_datagram(
            self, protocol_factory, local_addr, remote_addr, **kwargs)

    monkeypatch.setattr(loop_cls, "getaddrinfo", loop_gai)
    monkeypatch.setattr(loop_cls, "create_connection", create_connection)
    monkeypatch.setattr(loop_cls, "create_datagram_endpoint", create_datagram_endpoint)

    # Child processes: none (allowlist ALLOWED_CHILD_PROGRAMS).
    real_popen_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **kw):
        argv0 = args if isinstance(args, (str, bytes, os.PathLike)) else (args[0] if args else "")
        _check_child(argv0, args, kw.get("cwd"), env=kw.get("env"),
                     executable=kw.get("executable"), shell=bool(kw.get("shell")))
        return real_popen_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", popen_init)

    def guard_os(name, path_index=0):
        real = getattr(os, name, None)
        if real is None:
            return
        variadic = name.startswith(("execl", "spawnl"))
        has_env_last = variadic and name.endswith("e")

        def guarded(*args, **kwargs):
            target = args[path_index] if len(args) > path_index else ""
            try:
                prog = os.fsdecode(target)
            except TypeError:
                prog = str(target)
            rest = args[path_index + 1:]
            if name == "system":
                argv, via_shell = [prog], True
            elif variadic:
                argv = [os.fsdecode(x) for x in (rest[:-1] if has_env_last else rest)]
                via_shell = False
            else:
                try:
                    argv = [os.fsdecode(x) for x in rest[0]]
                except (IndexError, TypeError):
                    argv = [prog]
                via_shell = False
            _check_child(prog, argv or [prog], via_shell=via_shell)
            return real(*args, **kwargs)
        monkeypatch.setattr(os, name, guarded)

    guard_os("system")
    for fn in ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe", "execl",
               "execle", "execlp", "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
               "spawnl", "spawnle", "spawnlp", "spawnlpe"):
        guard_os(fn, 1 if fn.startswith("spawn") else 0)  # spawn*(mode, path, ...)
