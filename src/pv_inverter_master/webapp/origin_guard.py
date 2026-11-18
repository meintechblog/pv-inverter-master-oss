"""Reject cross-site writes to the JSON API.

The web interface has no login and is meant for a trusted LAN. That does
not protect against a page on another site that a LAN user happens to
open: the browser can send a "simple" cross-site request (``text/plain``,
form encoding or multipart, no preflight) to this host. This middleware
blocks such requests for every mutating ``/api/`` call:

1. ``Sec-Fetch-Site: cross-site`` -> 403.
2. ``Origin`` present but not the same host and port as ``Host`` -> 403
   (``Origin: null`` counts as foreign).
3. A request body sent as ``text/plain`` or
   ``application/x-www-form-urlencoded`` -> 415; ``multipart/form-data``
   only for the image upload routes. The dashboard sends JSON, YAML or
   ZIP with an explicit content type, which forces browsers into a CORS
   preflight for cross-site callers.

Requests without ``Origin`` (curl, scripts) pass, so local automation
keeps working.
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit

from aiohttp import web

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_SIMPLE_BODY_TYPES = frozenset({"text/plain", "application/x-www-form-urlencoded"})
_MULTIPART = "multipart/form-data"


def _split_host(value: str, default_port: int) -> tuple[str, int] | None:
    """Split a ``Host`` header value into (host, port); None if malformed."""
    try:
        parts = urlsplit("//" + value)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname.lower(), port if port is not None else default_port


def _origin_matches_host(origin: str, host_header: str) -> bool:
    if origin == "null":
        return False
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return False
    default_port = _DEFAULT_PORTS[parts.scheme]
    origin_pair = (parts.hostname.lower(), port if port is not None else default_port)
    host_pair = _split_host(host_header, default_port)
    return host_pair is not None and origin_pair == host_pair


def _forbidden(reason: str) -> web.Response:
    return web.json_response({"error": reason}, status=403)


@web.middleware
async def origin_guard_middleware(
    request: web.Request,
    handler: Callable[[web.Request], web.StreamResponse],
) -> web.StreamResponse:
    if request.method in _MUTATING and request.path.startswith("/api/"):
        if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return _forbidden("cross_site_request")
        origin = request.headers.get("Origin")
        if origin is not None and not _origin_matches_host(
            origin, request.headers.get("Host", "")
        ):
            return _forbidden("origin_mismatch")
        if request.body_exists:
            mimetype = request.content_type.lower()
            if mimetype in _SIMPLE_BODY_TYPES or (
                mimetype == _MULTIPART and not request.path.endswith("/image")
            ):
                return web.json_response(
                    {"error": "unsupported_content_type"}, status=415
                )
    return await handler(request)
