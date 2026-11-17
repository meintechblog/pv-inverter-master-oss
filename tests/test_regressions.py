"""Regression tests for review findings."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from tests.test_gateway_isolation import _slave

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    from pv_inverter_master import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "DISTRIBUTE_COALESCE_S", 0.01)
    monkeypatch.setattr(proxy_mod, "FLUSH_RETRY_BACKOFF_S", 0.01, raising=False)
    monkeypatch.setattr(proxy_mod, "FLUSH_MAX_RETRIES", 2, raising=False)


# ------------------------------------------------------------------- N1
async def test_value_arriving_during_last_failing_attempt_is_written():
    """A lower value requested while the final attempt runs must not be lost."""
    gate = asyncio.Event()
    calls = []

    async def distribute(pct, enabled):
        calls.append(pct)
        if len(calls) == 3:               # last allowed attempt (1 + 2 retries)
            await gate.wait()
            raise RuntimeError("down")
        if len(calls) < 3:
            raise RuntimeError("down")

    d = MagicMock()
    d.distribute = distribute
    slave, _ = _slave(d)
    await slave.async_setValues(6, 40154, [80])
    for _ in range(200):
        if len(calls) == 3:
            break
        await asyncio.sleep(0.005)
    assert len(calls) == 3
    await slave.async_setValues(6, 40154, [30])       # lower value, runner running
    gate.set()
    await asyncio.wait_for(slave.wait_distributions(), 5)
    assert 30.0 in calls[3:], calls                   # the lower value was tried


# --------------------------------------------------------------- N2 / N3
def _safe(code):
    from tests.conftest import _python_snippet_is_safe
    return _python_snippet_is_safe(code)


@pytest.mark.parametrize("code", [
    "from os import system; system('true')",
    "from os import popen",
    "from os import *",
    "from os import execv as e; e('/bin/sh', ['sh'])",
    "from socket import create_connection",
    "from pathlib import *",
    "import os as o; o.system('true')",
    "import os; o = os; o.system('true')",
    "import os; os.path.os.system('true')",
    "import pv_inverter_master.modbus_gateway",
    "import pv_inverter_master.modbus_gateway.x",
    "import pv_inverter_master",
    "from pv_inverter_master.modbus_gateway import ModbusGateway",
    "from pv_inverter_master import modbus_gateway",
    "import os.path, socket",
    "from . import x",
])
def test_snippet_guard_rejects_bypasses(code):
    assert not _safe(code)


@pytest.mark.parametrize("code", [
    "import sys, time\nprint('ok')",
    "import os\nprint(os.path.join('a', 'b'), os.getpid())",
    "from pathlib import Path\nprint(Path('x'))",
    "from pv_inverter_master.instance_lock import acquire_instance_lock\nacquire_instance_lock('x')",
    "import sys, time\nfrom pv_inverter_master.instance_lock import guard_from_argv\n"
    "sys.argv = ['v.py']\nguard_from_argv('v.py')\ntime.sleep(30)",
])
def test_snippet_guard_still_allows_the_real_snippets(code):
    assert _safe(code)


# ------------------------------------------------------------------- N5
async def test_locked_write_returns_clean_error_for_unresolved_host():
    from pv_inverter_master.modbus_gateway import GatewayUnresolved
    from pv_inverter_master.plugin import locked_write_power_limit

    class P:
        @property
        def gateway(self):
            raise GatewayUnresolved("nohost.invalid: host name cannot be resolved")

        async def write_power_limit(self, enable, limit_pct, *, force=False):
            return self.gateway      # real plugins touch .gateway first

    for lock in (None, asyncio.Lock()):
        res = await locked_write_power_limit(P(), True, 50.0, poll_lock=lock)
        assert res.success is False
        assert res.error == "host_unresolved"
        assert lock is None or not lock.locked()


def test_peek_gateway_tolerates_unresolved_and_missing():
    from pv_inverter_master.modbus_gateway import GatewayUnresolved, peek_gateway

    class P:
        @property
        def gateway(self):
            raise GatewayUnresolved("x")

    assert peek_gateway(P()) is None
    assert peek_gateway(object()) is None
