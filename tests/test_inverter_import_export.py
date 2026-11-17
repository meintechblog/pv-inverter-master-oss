"""Per-inverter import/export route tests.

Single inverter → downloadable YAML and back, without touching the rest of
the config or restarting the whole process. New uploads are appended +
started; an id match updates in place + restarts that one device.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from pv_inverter_master.config import Config, InverterEntry
from pv_inverter_master.webapp.routes_config import (
    _extract_inverter_dict,
    _inverter_export_filename,
    inverter_export_handler,
    inverter_import_handler,
)


class _FakeRequest:
    def __init__(self, app, body: bytes = b"", match_info: dict | None = None):
        self.app = app
        self._body = body
        self.match_info = match_info or {}

    async def read(self) -> bytes:
        return self._body


def _app_with(config: Config, tmp_path: Path) -> dict:
    cfg_path = str(tmp_path / "config.yaml")
    return {"config": config, "config_path": cfg_path, "app_ctx": SimpleNamespace()}


# ── helpers ────────────────────────────────────────────────────────────

def test_extract_inverter_dict_shapes():
    assert _extract_inverter_dict({"inverter": {"host": "1.2.3.4"}}) == {"host": "1.2.3.4"}
    assert _extract_inverter_dict({"inverters": [{"host": "5.6.7.8"}]}) == {"host": "5.6.7.8"}
    assert _extract_inverter_dict({"host": "9.9.9.9", "type": "hoymiles"})["type"] == "hoymiles"
    assert _extract_inverter_dict({"nonsense": 1}) is None
    assert _extract_inverter_dict("string") is None


def test_export_filename_is_safe():
    e = InverterEntry(name="Cabin / Süd", id="abc123")
    fn = _inverter_export_filename(e)
    assert fn.endswith(".yaml")
    assert "Cabin" in fn and "/" not in fn


# ── export ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_returns_yaml_attachment(tmp_path):
    cfg = Config()
    cfg.inverters = [InverterEntry(id="dev1", host="198.51.100.128", type="hoymiles", name="Cabin")]
    req = _FakeRequest(_app_with(cfg, tmp_path), match_info={"id": "dev1"})

    resp = await inverter_export_handler(req)

    assert resp.status == 200
    assert "attachment" in resp.headers["Content-Disposition"]
    parsed = yaml.safe_load(resp.body.decode())
    assert parsed["inverter"]["host"] == "198.51.100.128"
    assert parsed["inverter"]["id"] == "dev1"


@pytest.mark.asyncio
async def test_export_unknown_id_404(tmp_path):
    req = _FakeRequest(_app_with(Config(), tmp_path), match_info={"id": "nope"})
    resp = await inverter_export_handler(req)
    assert resp.status == 404


# ── import ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_import_new_inverter_appends_and_starts(tmp_path):
    cfg = Config()
    cfg.inverters = []
    app = _app_with(cfg, tmp_path)
    body = yaml.safe_dump({"inverter": {"type": "hoymiles", "host": "198.51.100.128", "name": "Cabin"}}).encode()
    req = _FakeRequest(app, body=body)

    with patch("pv_inverter_master.webapp.routes_config._reconfigure_active", new=AsyncMock()) as recon:
        resp = await inverter_import_handler(req)

    out = json.loads(resp.body)
    assert out["success"] is True and out["is_new"] is True
    assert len(cfg.inverters) == 1
    assert cfg.inverters[0].host == "198.51.100.128"
    # started with action="start"
    recon.assert_awaited()
    assert recon.await_args.kwargs.get("action") == "start"


@pytest.mark.asyncio
async def test_import_existing_id_updates_in_place(tmp_path):
    cfg = Config()
    cfg.inverters = [InverterEntry(id="dev1", host="1.1.1.1", type="hoymiles", name="Old")]
    app = _app_with(cfg, tmp_path)
    body = yaml.safe_dump({"inverter": {"id": "dev1", "type": "hoymiles", "host": "2.2.2.2", "name": "New"}}).encode()
    req = _FakeRequest(app, body=body)

    with patch("pv_inverter_master.webapp.routes_config._reconfigure_active", new=AsyncMock()):
        resp = await inverter_import_handler(req)

    out = json.loads(resp.body)
    assert out["success"] is True and out["is_new"] is False
    assert len(cfg.inverters) == 1, "must update in place, not append a duplicate"
    assert cfg.inverters[0].host == "2.2.2.2"
    assert cfg.inverters[0].name == "New"


@pytest.mark.asyncio
async def test_import_rejects_bad_type(tmp_path):
    app = _app_with(Config(), tmp_path)
    body = yaml.safe_dump({"inverter": {"type": "frobnicator", "host": "1.2.3.4"}}).encode()
    req = _FakeRequest(app, body=body)
    resp = await inverter_import_handler(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_import_rejects_junk_file(tmp_path):
    app = _app_with(Config(), tmp_path)
    req = _FakeRequest(app, body=b"just some text, not a config")
    resp = await inverter_import_handler(req)
    assert resp.status == 400
