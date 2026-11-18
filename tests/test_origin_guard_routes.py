"""Every mutating route of the real app is covered by origin_guard_middleware.

Reads the routes from the app router, so a new route outside /api/ or one
that slips past the guard makes this test fail.
"""
from __future__ import annotations

import re

from tests.test_webapp import client, mock_config, shared_ctx  # noqa: F401  (fixtures)

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _routes(app):
    out = []
    for route in app.router.routes():
        if route.method not in MUTATING:
            continue
        info = route.resource.get_info()
        path = info.get("path") or info.get("formatter")
        out.append((route.method, re.sub(r"\{[^}]+\}", "1", path)))
    return out


async def test_all_mutating_routes_are_under_api(client):
    routes = _routes(client.server.app)
    assert routes, "no routes found"
    assert [r for r in routes if not r[1].startswith("/api/")] == []


async def test_all_mutating_routes_reject_foreign_origin(client):
    wrong = []
    for method, path in _routes(client.server.app):
        for headers in ({"Origin": "http://attacker.example"}, {"Sec-Fetch-Site": "cross-site"}):
            resp = await client.request(method, path, json={}, headers=headers)
            if resp.status != 403:
                wrong.append((method, path, headers, resp.status))
    assert wrong == []


async def test_all_mutating_routes_reject_text_plain(client):
    wrong = []
    for method, path in _routes(client.server.app):
        resp = await client.request(method, path, data="{}", headers={"Content-Type": "text/plain"})
        if resp.status != 415:
            wrong.append((method, path, resp.status))
    assert wrong == []
