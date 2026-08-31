# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed ASGI surface for the optional OpenBB sidecar."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Final
from urllib.parse import parse_qsl

from starlette.types import ASGIApp, Receive, Scope, Send

from stonks_service_auth import (
    ServiceAccessTarget,
    ServiceAdmissionMiddleware,
    ServiceAdmissionResponseStyle,
    ServiceAuthenticator,
    ServicePermission,
    ServiceReceiver,
    ServiceResourceKind,
    authorize_service_dispatch,
    exactly_one_authorization_header,
)

type MarketDispatch = tuple[ServiceAccessTarget, dict[str, object]]

SOURCE_LINK: Final = '</source>; rel="source"; type="application/gzip"'
HISTORICAL_PATH: Final = "/api/v1/equity/price/historical"
ALLOWED_HTTP_SURFACE: Final = (
    ("GET", "/api/v1/equity/price/historical"),
    ("GET", "/healthz"),
    ("GET", "/source"),
)
ANONYMOUS_HTTP_SURFACE: Final = frozenset(
    {
        ("GET", "/healthz"),
        ("GET", "/source"),
    }
)
_PROTECTED_HTTP_SURFACE: Final = frozenset({("GET", HISTORICAL_PATH)})
_ALLOWED_QUERY_FIELDS: Final = frozenset(
    {"symbol", "start_date", "end_date", "interval", "provider"}
)
_ALLOWED_INTERVALS: Final = frozenset(
    {"1m", "2m", "5m", "15m", "30m", "90m", "1h", "1d", "1W", "1M"}
)
_SYMBOL: Final = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
_SUFFIX_MARKETS: Final = ((".TW", "TW"), (".TWO", "TW"), (".HK", "HK"))
_MAX_QUERY_BYTES: Final = 4096
_NOT_FOUND_BODY: Final = b'{"detail":"Not Found"}'
_BAD_REQUEST_BODY: Final = b'{"detail":"Invalid market request"}'
_UNAUTHORIZED_BODY: Final = b'{"detail":"Service authentication failed"}'
_FORBIDDEN_BODY: Final = b'{"detail":"Service target access denied"}'


def build_surface(
    app: ASGIApp,
    *,
    authenticator: ServiceAuthenticator,
) -> ASGIApp:
    """Compose pre-auth admission outside the governed OpenBB allowlist."""

    return ServiceAdmissionMiddleware(
        SurfaceAllowlist(app, authenticator=authenticator),
        response_style=ServiceAdmissionResponseStyle.OPENBB,
        extra_response_headers=((b"link", SOURCE_LINK.encode("ascii")),),
    )


class SurfaceAllowlist:
    """Expose only the governed read-only routes before OpenBB routing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        authenticator: ServiceAuthenticator,
    ) -> None:
        self._app = app
        self._authenticator = authenticator

    async def __call__(
        self,
        scope: Scope,
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
        if route in ANONYMOUS_HTTP_SURFACE:
            await self._app(scope, receive, send)
            return
        if route in _PROTECTED_HTTP_SURFACE:
            await self._protected(scope, receive, send)
            return
        await self._not_found(send)

    async def _protected(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        authorization = _authorization_from_scope(scope)
        principal = self._authenticator.authenticate(authorization)
        if principal is None:
            await _response(
                send,
                status=401,
                body=_UNAUTHORIZED_BODY,
                authenticate=True,
            )
            return
        dispatch = _target_from_scope(scope)
        if dispatch is None:
            await _response(send, status=400, body=_BAD_REQUEST_BODY)
            return
        target, request_payload = dispatch
        if not authorize_service_dispatch(
            principal,
            permission=ServicePermission.DISPATCH_ASSIGNED_MARKET_DATA,
            target=target,
            receiver=ServiceReceiver.OPENBB,
            attempt_generation=0,
            attempt_nonce="",
            request_payload=request_payload,
            deadline=None,
        ):
            await _response(send, status=403, body=_FORBIDDEN_BODY)
            return
        await self._app(scope, receive, send)

    @staticmethod
    async def _not_found(send: Send) -> None:
        await _response(send, status=404, body=_NOT_FOUND_BODY)


def _authorization_from_scope(scope: Scope) -> str | None:
    raw_headers = scope.get("headers")
    if not isinstance(raw_headers, (list, tuple)):
        return None
    headers: list[tuple[bytes, bytes]] = []
    for item in raw_headers:
        if not (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
        ):
            return None
        headers.append((item[0], item[1]))
    return exactly_one_authorization_header(headers)


def _target_from_scope(scope: Scope) -> MarketDispatch | None:
    raw = scope.get("query_string")
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= _MAX_QUERY_BYTES:
        return None
    try:
        pairs = parse_qsl(
            raw.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    if len(pairs) > len(_ALLOWED_QUERY_FIELDS):
        return None
    query = dict(pairs)
    if (
        len(query) != len(pairs)
        or not set(query).issubset(_ALLOWED_QUERY_FIELDS)
        or query.get("provider") != "yfinance"
    ):
        return None
    symbol = query.get("symbol", "")
    if (
        _SYMBOL.fullmatch(symbol) is None
        or not _valid_dates(query)
        or query.get("interval", "1d") not in _ALLOWED_INTERVALS
    ):
        return None
    return (
        ServiceAccessTarget(
            kind=ServiceResourceKind.MARKET,
            identifier=f"{_market_for_symbol(symbol)}/{symbol}",
        ),
        {
            "method": "GET",
            "path": HISTORICAL_PATH,
            "query": query,
        },
    )


def _market_for_symbol(symbol: str) -> str:
    """Mirror of stonks_agent.domain.market_region.market_for_symbol.

    This sidecar is an isolated AGPL project and cannot import from core, so the
    two must be kept identical; tests/policy asserts they agree.
    """

    for suffix, market in _SUFFIX_MARKETS:
        if symbol.endswith(suffix):
            return market
    return "US"


def _valid_dates(query: Mapping[str, str]) -> bool:
    try:
        start = (
            date.fromisoformat(query["start_date"]) if "start_date" in query else None
        )
        end = date.fromisoformat(query["end_date"]) if "end_date" in query else None
    except ValueError:
        return False
    return start is None or end is None or start <= end


async def _response(
    send: Send,
    *,
    status: int,
    body: bytes,
    authenticate: bool = False,
) -> None:
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
        (b"link", SOURCE_LINK.encode("ascii")),
    ]
    if authenticate:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
