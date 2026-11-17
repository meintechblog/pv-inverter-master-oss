"""Single-instance lock: a second proxy (or a direct-access script) is refused."""
import os
import subprocess
import sys
import textwrap

import pytest

from pv_inverter_master import instance_lock
from pv_inverter_master.instance_lock import (
    InstanceAlreadyRunning,
    acquire_instance_lock,
    guard_from_argv,
    running_instance_pid,
)


@pytest.fixture
def lockfile(tmp_path, monkeypatch):
    path = str(tmp_path / "instance.lock")
    monkeypatch.setenv(instance_lock.ENV_LOCK_PATH, path)
    monkeypatch.setattr(instance_lock, "_held", None)
    return path


def _hold_in_child(path):
    code = textwrap.dedent(f"""
        import sys, time
        from pv_inverter_master.instance_lock import acquire_instance_lock
        acquire_instance_lock({path!r})
        print("held", flush=True)
        time.sleep(30)
    """)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, env=env)
    assert child.stdout.readline().strip() == b"held"
    return child


def test_second_instance_is_refused_and_freed_on_exit(lockfile):
    child = _hold_in_child(lockfile)
    try:
        assert running_instance_pid(lockfile) == str(child.pid)
        with pytest.raises(InstanceAlreadyRunning):
            acquire_instance_lock(lockfile)
    finally:
        child.kill()
        child.wait()
    # holder died -> lock is free again (no stale lock after a crash)
    assert running_instance_pid(lockfile) is None
    acquire_instance_lock(lockfile)
    assert running_instance_pid(lockfile) == str(os.getpid())


def test_no_lockfile_means_not_running(lockfile):
    assert running_instance_pid(lockfile) is None


def test_script_guard_refuses_next_to_service(lockfile, monkeypatch):
    child = _hold_in_child(lockfile)
    try:
        monkeypatch.setattr(sys, "argv", ["script.py"])
        with pytest.raises(SystemExit) as exc:
            guard_from_argv("script.py")
        assert exc.value.code == 2
        # explicit override: warns, strips the flag, does not exit
        monkeypatch.setattr(sys, "argv", ["script.py", "--force-parallel"])
        guard_from_argv("script.py")
        assert sys.argv == ["script.py"]
    finally:
        child.kill()
        child.wait()


def test_script_guard_passes_when_service_is_down(lockfile, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["script.py"])
    guard_from_argv("script.py")
