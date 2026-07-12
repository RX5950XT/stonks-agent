# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed ASGI surface for the optional OpenBB sidecar."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

type ASGIMessage = dict[str, object]
type ASGIScope = Mapping[str, object]
type Receive = Callable[[], Awaitable[ASGIMessage]]
type Send = Callable[[ASGIMessage], Awaitable[None]]
type ASGIApp = Callable[[ASGIScope, Receive, Send], Awaitable[None]]

SOURCE_LINK: Final = '</source>; rel="source"; type="application/gzip"'
ALLOWED_HTTP_SURFACE: Final = (
    ("GET", "/api/v1/equity/price/historical"),
    ("GET", "/healthz"),
    ("GET", "/source"),
)
_NOT_FOUND_BODY: Final = b'{"detail":"Not Found"}'


class SurfaceAllowlist:
    """Expose only the governed read-only routes before OpenBB routing."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: ASGIScope,
        receive: Receive,
        send: Send,
    ) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope_type != "http":
            return
        route = (scope.get("method"), scope.get("path"))
        if route in ALLOWED_HTTP_SURFACE:
            await self._app(scope, receive, send)
            return
        await self._not_found(send)

    @staticmethod
    async def _not_found(send: Send) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(_NOT_FOUND_BODY)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"link", SOURCE_LINK.encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": 404, "headers": headers})
        await send({"type": "http.response.body", "body": _NOT_FOUND_BODY})
