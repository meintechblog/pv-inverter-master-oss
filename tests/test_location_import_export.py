"""Whole-Standort import/export (ZIP) route tests.

A Standort exports to a ZIP bundling standort.yaml (location + all its
inverters) and the photo; import restores all three — new, or update by id.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from pv_inverter_master.config import Config, InverterEntry, Location
from pv_inverter_master.webapp.routes_locations import (
    location_export_handler,
    location_import_handler,
)


class _FakeRequest:
    def __init__(self, app, body: bytes = b"", match_info: dict | None = None):
        self.app = app
        self._body = body
        self.match_info = match_info or {}

    async def read(self) -> bytes:
        return self._body


def _app(config: Config, tmp_path: Path) -> dict:
    return {"config": config, "config_path": str(tmp_path / "config.yaml"), "app_ctx": SimpleNamespace()}


def _make_zip(location: dict, inverters: list, image: tuple | None = None) -> bytes:
    manifest = {"_export": "pv-inverter-master-location", "version": 1,
                "location": location, "inverters": inverters}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("standort.yaml", yaml.safe_dump(manifest))
        if image:
            zf.writestr(image[0], image[1])
    return buf.getvalue()


# ── export ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_bundles_location_inverters_and_image(tmp_path):
    cfg = Config()
    cfg.locations = [Location(id="loc1", name="Workshop", image_filename="loc1.png")]
    cfg.inverters = [
        InverterEntry(id="inv1", host="1.1.1.1", type="hoymiles", location_id="loc1"),
        InverterEntry(id="inv2", host="2.2.2.2", type="opendtu", location_id="other"),
    ]
    # Place the image where the handler expects it.
    img_dir = tmp_path / "location-images"
    img_dir.mkdir()
    (img_dir / "loc1.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")

    resp = await location_export_handler(_FakeRequest(_app(cfg, tmp_path), match_info={"id": "loc1"}))

    assert resp.status == 200
    assert resp.content_type == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.body))
    manifest = yaml.safe_load(zf.read("standort.yaml"))
    assert manifest["location"]["name"] == "Workshop"
    assert [i["id"] for i in manifest["inverters"]] == ["inv1"], "only this Standort's inverters"
    assert "loc1.png" in zf.namelist()


@pytest.mark.asyncio
async def test_export_unknown_location_404(tmp_path):
    resp = await location_export_handler(_FakeRequest(_app(Config(), tmp_path), match_info={"id": "nope"}))
    assert resp.status == 404


# ── import ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_import_new_location_with_inverters_and_image(tmp_path):
    cfg = Config()
    cfg.locations, cfg.inverters = [], []
    app = _app(cfg, tmp_path)
    body = _make_zip(
        location={"id": "loc1", "name": "Workshop"},
        inverters=[{"id": "inv1", "type": "hoymiles", "host": "1.1.1.1", "location_id": "loc1"}],
        image=("loc1.png", b"\x89PNG\r\n\x1a\nFAKE"),
    )
    req = _FakeRequest(app, body=body)

    with patch("pv_inverter_master.webapp.routes_locations._reconfigure_active", new=AsyncMock()), \
         patch("pv_inverter_master.webapp.routes_locations._reload_location_mqtt", new=AsyncMock()), \
         patch("pv_inverter_master.webapp.routes_locations.broadcast_location_list", new=AsyncMock()), \
         patch("pv_inverter_master.webapp.routes_locations.broadcast_device_list", new=AsyncMock()):
        resp = await location_import_handler(req)

    out = json.loads(resp.body)
    assert out["success"] is True and out["is_new"] is True
    assert out["inverters_imported"] == 1
    assert len(cfg.locations) == 1 and cfg.locations[0].name == "Workshop"
    assert len(cfg.inverters) == 1 and cfg.inverters[0].location_id == "loc1"
    # Image restored as <loc_id><ext>
    assert (tmp_path / "location-images" / "loc1.png").is_file()
    assert cfg.locations[0].image_filename == "loc1.png"


@pytest.mark.asyncio
async def test_import_existing_id_updates_in_place(tmp_path):
    cfg = Config()
    cfg.locations = [Location(id="loc1", name="Old Name")]
    cfg.inverters = []
    app = _app(cfg, tmp_path)
    body = _make_zip(location={"id": "loc1", "name": "New Name"}, inverters=[])
    req = _FakeRequest(app, body=body)

    with patch("pv_inverter_master.webapp.routes_locations._reconfigure_active", new=AsyncMock()), \
         patch("pv_inverter_master.webapp.routes_locations._reload_location_mqtt", new=AsyncMock()), \
         patch("pv_inverter_master.webapp.routes_locations.broadcast_location_list", new=AsyncMock()), \
         patch("pv_inverter_master.webapp.routes_locations.broadcast_device_list", new=AsyncMock()):
        resp = await location_import_handler(req)

    out = json.loads(resp.body)
    assert out["is_new"] is False
    assert len(cfg.locations) == 1 and cfg.locations[0].name == "New Name"


@pytest.mark.asyncio
async def test_import_skips_invalid_inverter_not_fatal(tmp_path):
    cfg = Config()
    cfg.locations, cfg.inverters = [], []
    app = _app(cfg, tmp_path)
    body = _make_zip(
        location={"id": "loc1", "name": "Workshop"},
        inverters=[
            {"id": "good", "type": "hoymiles", "host": "1.1.1.1"},
            {"id": "bad", "type": "frobnicator", "host": "2.2.2.2"},  # invalid type → skipped
        ],
    )
    req = _FakeRequest(app, body=body)

    with patch("pv_inverter_master.webapp.routes_locations._reconfigure_active", new=AsyncMock()), \
         patch("pv_inverter_master.webapp.routes_locations._reload_location_mqtt", new=AsyncMock()), \
         patch("pv_inverter_master.webapp.routes_locations.broadcast_location_list", new=AsyncMock()), \
         patch("pv_inverter_master.webapp.routes_locations.broadcast_device_list", new=AsyncMock()):
        resp = await location_import_handler(req)

    out = json.loads(resp.body)
    assert out["success"] is True
    assert out["inverters_imported"] == 1 and out["inverters_skipped"] == 1


@pytest.mark.asyncio
async def test_import_rejects_non_zip(tmp_path):
    req = _FakeRequest(_app(Config(), tmp_path), body=b"not a zip at all")
    resp = await location_import_handler(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_import_rejects_zip_without_manifest(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("random.txt", "hi")
    req = _FakeRequest(_app(Config(), tmp_path), body=buf.getvalue())
    resp = await location_import_handler(req)
    assert resp.status == 400
