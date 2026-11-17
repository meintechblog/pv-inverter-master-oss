"""Nightly auto-update loop: timing + trigger-write gating."""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pv_inverter_master.updater import auto_update
from pv_inverter_master.updater.config import UpdateConfig


def test_seconds_until_next_before_target():
    now = datetime.datetime(2026, 5, 30, 3, 0, 0)
    assert auto_update.seconds_until_next(4, now) == 3600.0


def test_seconds_until_next_after_target_rolls_to_tomorrow():
    now = datetime.datetime(2026, 5, 30, 5, 0, 0)
    # 23h until 04:00 next day
    assert auto_update.seconds_until_next(4, now) == 23 * 3600.0


def test_seconds_until_next_exactly_on_hour_rolls():
    now = datetime.datetime(2026, 5, 30, 4, 0, 0)
    assert auto_update.seconds_until_next(4, now) == 24 * 3600.0


def _ctx(available):
    return SimpleNamespace(available_update=available)


SHA = "c" * 40


@pytest.mark.asyncio
async def test_triggers_install_when_enabled_and_available(tmp_path):
    ctx = _ctx({"latest_version": "v8.1.0", "latest_commit": SHA})
    scheduler = SimpleNamespace(check_once=AsyncMock())
    with patch.object(auto_update, "load_update_config",
                      return_value=UpdateConfig(enabled=True, auto_install=True)), \
         patch.object(auto_update, "write_trigger") as wt, \
         patch("pv_inverter_master.updater.maintenance.enter_maintenance_mode", new=AsyncMock()):
        out = await auto_update.run_auto_update_once(ctx, str(tmp_path / "c.yaml"), scheduler)

    assert out is True
    wt.assert_called_once()
    payload = wt.call_args.args[0]
    assert payload.op == "update" and payload.target_sha == SHA
    assert payload.requested_by == "auto-update"


@pytest.mark.asyncio
async def test_skips_when_auto_install_off(tmp_path):
    ctx = _ctx({"latest_version": "v8.1.0", "latest_commit": SHA})
    with patch.object(auto_update, "load_update_config",
                      return_value=UpdateConfig(enabled=True, auto_install=False)), \
         patch.object(auto_update, "write_trigger") as wt:
        out = await auto_update.run_auto_update_once(ctx, str(tmp_path / "c.yaml"), None)
    assert out is False
    wt.assert_not_called()


@pytest.mark.asyncio
async def test_skips_when_no_update(tmp_path):
    ctx = _ctx(None)
    with patch.object(auto_update, "load_update_config",
                      return_value=UpdateConfig(enabled=True, auto_install=True)), \
         patch.object(auto_update, "write_trigger") as wt:
        out = await auto_update.run_auto_update_once(ctx, str(tmp_path / "c.yaml"), None)
    assert out is False
    wt.assert_not_called()


@pytest.mark.asyncio
async def test_skips_when_no_target_sha(tmp_path):
    ctx = _ctx({"latest_version": "v8.1.0", "latest_commit": None})
    with patch.object(auto_update, "load_update_config",
                      return_value=UpdateConfig(enabled=True, auto_install=True)), \
         patch.object(auto_update, "write_trigger") as wt:
        out = await auto_update.run_auto_update_once(ctx, str(tmp_path / "c.yaml"), None)
    assert out is False
    wt.assert_not_called()


def test_auto_install_defaults_on():
    assert UpdateConfig().auto_install is True
