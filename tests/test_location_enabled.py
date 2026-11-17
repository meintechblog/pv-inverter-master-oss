"""Deactivate/activate a whole Standort → cascade to its inverters."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pv_inverter_master.config import Config, InverterEntry, Location
from pv_inverter_master.webapp.routes_locations import location_set_enabled_handler


class _FakeRequest:
    def __init__(self, app, body: dict | None = None, match_info: dict | None = None):
        self.app = app
        self._body = body or {}
        self.match_info = match_info or {}

    async def json(self):
        return self._body


def _app(config: Config, tmp_path: Path, active_ids=None) -> dict:
    registry = SimpleNamespace(get_active_device_ids=lambda: list(active_ids or []))
    app_ctx = SimpleNamespace(device_registry=registry)
    return {"config": config, "config_path": str(tmp_path / "config.yaml"), "app_ctx": app_ctx}


def _patches():
    return (
        patch("pv_inverter_master.webapp.routes_locations._reconfigure_active", new=AsyncMock()),
        patch("pv_inverter_master.webapp.routes_locations.broadcast_location_list", new=AsyncMock()),
        patch("pv_inverter_master.webapp.routes_locations.broadcast_device_list", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_disable_location_cascades_to_inverters(tmp_path):
    cfg = Config()
    cfg.locations = [Location(id="loc1", name="Workshop", enabled=True)]
    cfg.inverters = [
        InverterEntry(id="a", host="1.1.1.1", type="hoymiles", location_id="loc1", enabled=True),
        InverterEntry(id="b", host="2.2.2.2", type="opendtu", location_id="loc1", enabled=True),
        InverterEntry(id="c", host="3.3.3.3", type="solaredge", location_id="other", enabled=True),
    ]
    # a + b currently running.
    app = _app(cfg, tmp_path, active_ids=["a", "b"])
    req = _FakeRequest(app, body={"enabled": False}, match_info={"id": "loc1"})

    p1, p2, p3 = _patches()
    with p1 as recon, p2, p3:
        resp = await location_set_enabled_handler(req)

    out = json.loads(resp.body)
    assert out["success"] is True and out["enabled"] is False
    assert out["inverters_affected"] == 2
    # Location + its inverters disabled; the other-location inverter untouched.
    assert cfg.locations[0].enabled is False
    assert cfg.inverters[0].enabled is False and cfg.inverters[1].enabled is False
    assert cfg.inverters[2].enabled is True
    # Both active devices were stopped.
    actions = [c.kwargs.get("action") for c in recon.await_args_list]
    assert actions == ["stop", "stop"]


@pytest.mark.asyncio
async def test_enable_location_starts_only_stopped_devices(tmp_path):
    cfg = Config()
    cfg.locations = [Location(id="loc1", name="Workshop", enabled=False)]
    cfg.inverters = [
        InverterEntry(id="a", host="1.1.1.1", type="hoymiles", location_id="loc1", enabled=False),
        InverterEntry(id="b", host="2.2.2.2", type="opendtu", location_id="loc1", enabled=False),
    ]
    # Neither running yet (location was disabled).
    app = _app(cfg, tmp_path, active_ids=[])
    req = _FakeRequest(app, body={"enabled": True}, match_info={"id": "loc1"})

    p1, p2, p3 = _patches()
    with p1 as recon, p2, p3:
        resp = await location_set_enabled_handler(req)

    out = json.loads(resp.body)
    assert out["enabled"] is True and out["inverters_affected"] == 2
    assert cfg.locations[0].enabled is True
    assert all(i.enabled for i in cfg.inverters)
    actions = [c.kwargs.get("action") for c in recon.await_args_list]
    assert actions == ["start", "start"]


@pytest.mark.asyncio
async def test_toggle_unknown_location_404(tmp_path):
    app = _app(Config(), tmp_path)
    req = _FakeRequest(app, body={"enabled": False}, match_info={"id": "nope"})
    resp = await location_set_enabled_handler(req)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_location_enabled_field_roundtrips(tmp_path):
    from pv_inverter_master.config import load_config, save_config
    cfg = Config()
    cfg.locations = [Location(id="loc1", name="X", enabled=False)]
    p = str(tmp_path / "config.yaml")
    save_config(p, cfg)
    reloaded = load_config(p)
    assert reloaded.locations[0].enabled is False
