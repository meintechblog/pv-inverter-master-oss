"""Process-wide single-instance lock (flock) for the proxy service.

Why: the SE30K takes exactly one Modbus session. A second proxy process (a
zombie old instance after a botched restart, a manual ``python -m
pv_inverter_master`` next to the systemd unit) would hold a second session and
wedge it (incident 2026-07-16). The OS releases an flock when the holder dies,
so a crashed instance never leaves a stale lock behind.

Scripts that talk to an inverter directly use :func:`guard_from_argv` to refuse
to run next to the service.
"""
from __future__ import annotations

import errno
import fcntl
import os
import sys
from contextlib import contextmanager

#: systemd ``RuntimeDirectory=pv-inverter-master`` creates this, owned by the
#: service user. Override with PV_INVERTER_MASTER_LOCK (tests, dev boxes).
SERVICE_LOCK_PATH = "/run/pv-inverter-master/instance.lock"
ENV_LOCK_PATH = "PV_INVERTER_MASTER_LOCK"

_held = None  # keeps the lock fd alive for the life of the process


class InstanceAlreadyRunning(RuntimeError):
    """Another proxy instance holds the lock."""


class InstanceLockUnavailable(RuntimeError):
    """The lock file cannot be opened/created: no mutual exclusion is possible."""


def lock_path() -> str:
    """The ONE lock file every process on the box must use.

    Intentionally no per-user fallback: a different path for a different user
    would let the service and a manual instance both "win" and open two
    sessions. If the fixed path is unusable, callers abort instead.
    """
    return os.environ.get(ENV_LOCK_PATH) or SERVICE_LOCK_PATH


def _open_lock_file(path: str, *, create: bool) -> tuple[int, bool]:
    """Open the lock file; returns (fd, writable). Raises InstanceLockUnavailable."""
    try:
        fd = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0), 0o666)
        try:
            os.fchmod(fd, 0o666)  # other users may take the (read-only) flock
        except OSError:
            pass
        return fd, True
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EPERM):
            try:  # flock needs no write permission: a read-only fd locks fine
                return os.open(path, os.O_RDONLY), False
            except OSError:
                pass
        raise InstanceLockUnavailable(
            f"cannot open instance lock {path}: {e.strerror or e}"
        ) from None


def _read_pid(fd: int) -> str:
    try:
        return os.pread(fd, 32, 0).decode().strip() or "?"
    except OSError:
        return "?"


def acquire_instance_lock(path: str | None = None) -> None:
    """Take the exclusive lock or raise.

    :class:`InstanceAlreadyRunning` - another process holds it.
    :class:`InstanceLockUnavailable` - the fixed lock path cannot be opened
    (e.g. missing RuntimeDirectory): the caller must abort, never fall back.
    """
    global _held
    path = path or lock_path()
    fd, writable = _open_lock_file(path, create=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        pid = _read_pid(fd)
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise InstanceAlreadyRunning(
                f"another pv-inverter-master instance holds {path} (pid {pid})"
            ) from None
        raise
    if writable:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    _held = fd


def running_instance_pid(path: str | None = None) -> str | None:
    """PID text of the instance holding the lock, or None if nobody does."""
    path = path or lock_path()
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return _read_pid(fd)
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


@contextmanager
def try_exclusive_lock(path: str | None = None):
    """Hold the instance lock for the duration of a ``with`` block, if free.

    Yields True when the exclusive lock was taken (no proxy runs: a direct
    connection to an inverter cannot collide with its session; a proxy starting
    meanwhile finds the lock taken and waits for the next start). Yields False
    when the proxy holds the lock or the lock path is unusable: the caller must
    NOT open any connection to an inverter. The lock is released on exit.
    """
    path = path or lock_path()
    try:
        fd, _writable = _open_lock_file(path, create=True)
    except InstanceLockUnavailable:
        yield False
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def guard_from_argv(script: str) -> None:
    """For scripts that open their own Modbus session to an inverter.

    Takes the SAME exclusive lock the service uses and HOLDS it for the life of
    the process, so the service cannot start (it would find the lock taken and
    refuse) while the script talks to the inverter, and vice versa. Exits with
    code 2 while the service runs or when the lock cannot be taken at all,
    unless ``--force-parallel`` is on the command line (removed from
    ``sys.argv``; the override prints a loud warning because it recreates the
    wedge scenario).
    """
    forced = "--force-parallel" in sys.argv
    if forced:
        sys.argv.remove("--force-parallel")
    try:
        acquire_instance_lock()
        return  # lock held until this process exits
    except InstanceAlreadyRunning as exc:
        reason = str(exc)
        pid = running_instance_pid() or "?"
    except (InstanceLockUnavailable, OSError) as exc:
        reason = str(exc)
        pid = None
    if forced:
        print(
            f"WARNING: {script}: {reason}; --force-parallel opens a Modbus "
            "session WITHOUT the instance lock and can wedge a single-session inverter.",
            file=sys.stderr,
        )
        return
    if pid is None:
        print(f"REFUSED: {script}: {reason}. No mutual exclusion possible.", file=sys.stderr)
    else:
        print(
            f"REFUSED: {script} talks to an inverter directly, but pv-inverter-master is "
            f"running (pid {pid}). A second Modbus session can wedge the SE30K. Stop the "
            "service first (systemctl stop pv-inverter-master) or pass --force-parallel.",
            file=sys.stderr,
        )
    sys.exit(2)
