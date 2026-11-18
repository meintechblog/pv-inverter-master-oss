"""Cross-site write protection for the JSON API (origin_guard_middleware)."""
from __future__ import annotations

import pytest

from pv_inverter_master.webapp.origin_guard import _origin_matches_host
from tests.test_webapp import client, mock_config, shared_ctx  # noqa: F401  (fixtures)

FOREIGN = "http://attacker.example"
LIMIT_BODY = '{"action": "set", "limit_pct": 7.0}'


def _same_origin(client) -> str:
    return f"http://{client.server.host}:{client.server.port}"


async def test_cross_site_text_plain_power_limit_rejected(client, shared_ctx):
    before = shared_ctx.control_state.wmaxlimpct_raw
    resp = await client.post(
        "/api/power-limit",
        data=LIMIT_BODY,
        headers={"Content-Type": "text/plain", "Origin": FOREIGN},
    )
    assert resp.status == 403
    assert shared_ctx.control_state.wmaxlimpct_raw == before


async def test_same_origin_json_power_limit_allowed(client, shared_ctx):
    resp = await client.post(
        "/api/power-limit",
        json={"action": "set", "limit_pct": 7.0},
        headers={"Origin": _same_origin(client)},
    )
    assert resp.status == 200
    assert shared_ctx.control_state.wmaxlimpct_raw == 7


async def test_no_origin_json_allowed(client, shared_ctx):
    """curl/scripts send no Origin header and keep working."""
    resp = await client.post("/api/power-limit", json={"action": "set", "limit_pct": 9.0})
    assert resp.status == 200
    assert shared_ctx.control_state.wmaxlimpct_raw == 9


async def test_same_origin_text_plain_rejected_415(client, shared_ctx):
    before = shared_ctx.control_state.wmaxlimpct_raw
    resp = await client.post(
        "/api/power-limit",
        data=LIMIT_BODY,
        headers={"Content-Type": "text/plain", "Origin": _same_origin(client)},
    )
    assert resp.status == 415
    assert shared_ctx.control_state.wmaxlimpct_raw == before


async def test_form_encoded_without_origin_rejected_415(client):
    resp = await client.post(
        "/api/power-limit",
        data="action=set&limit_pct=7",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status == 415


async def test_multipart_outside_image_route_rejected_415(client):
    resp = await client.post(
        "/api/power-limit",
        data=b"--x\r\n\r\n--x--\r\n",
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )
    assert resp.status == 415


async def test_sec_fetch_site_cross_site_rejected(client):
    resp = await client.post(
        "/api/power-limit",
        json={"action": "set", "limit_pct": 7.0},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status == 403


async def test_origin_null_rejected(client):
    resp = await client.post(
        "/api/power-limit",
        json={"action": "set", "limit_pct": 7.0},
        headers={"Origin": "null"},
    )
    assert resp.status == 403


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_every_mutating_method_checked(client, method):
    resp = await getattr(client, method)(
        "/api/config", json={}, headers={"Origin": FOREIGN}
    )
    assert resp.status == 403


async def test_update_route_foreign_origin_rejected_before_csrf(client):
    resp = await client.patch(
        "/api/update/config",
        data='{"auto_install": true}',
        headers={"Content-Type": "text/plain", "Origin": FOREIGN},
    )
    assert resp.status == 403


async def test_get_not_affected(client):
    resp = await client.get("/api/config", headers={"Origin": FOREIGN})
    assert resp.status == 200


@pytest.mark.parametrize(
    ("origin", "host", "expected"),
    [
        ("http://192.0.2.5", "192.0.2.5", True),
        ("http://192.0.2.5", "192.0.2.5:80", True),
        ("http://192.0.2.5:8080", "192.0.2.5:8080", True),
        ("http://pv.local", "PV.local", True),
        ("https://192.0.2.5", "192.0.2.5", True),  # same host, port from scheme
        ("http://192.0.2.5:8080", "192.0.2.5", False),
        ("http://evil.example", "192.0.2.5", False),
        ("http://[2001:db8::1]", "[2001:db8::1]", True),
        ("null", "192.0.2.5", False),
        ("file://", "192.0.2.5", False),
        ("http://192.0.2.5", "", False),
        ("http://192.0.2.5:99999", "192.0.2.5", False),
    ],
)
def test_origin_matches_host(origin, host, expected):
    assert _origin_matches_host(origin, host) is expected


@pytest.mark.parametrize(
    "content_type",
    ["text/plain; charset=utf-8", "TEXT/Plain", "application/x-www-form-urlencoded; charset=UTF-8"],
)
async def test_simple_content_type_variants_rejected_415(client, content_type):
    resp = await client.post(
        "/api/power-limit", data=LIMIT_BODY, headers={"Content-Type": content_type}
    )
    assert resp.status == 415


async def test_multipart_on_image_route_passes_guard(client):
    """The image upload keeps working: the guard lets multipart through on /image."""
    resp = await client.post(
        "/api/locations/does-not-exist/image",
        data=b"--x\r\n\r\n--x--\r\n",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Origin": _same_origin(client),
        },
    )
    assert resp.status not in (403, 415)
