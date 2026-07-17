"""Bounded deterministic two-stage API rate limiting."""

from __future__ import annotations

import hashlib
import heapq
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from stonks_agent.domain.auth import LocalPrincipal
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError
from stonks_agent.domain.rate_limit import RateLimitDecision
from stonks_agent.entrypoints.api.envelope import error_envelope
from stonks_agent.ports.authentication import AuthenticationRequest, Authenticator
from stonks_agent.ports.rate_limit_store import RateLimitStore

type Clock = Callable[[], datetime]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_KEY_LENGTH = 256
_MAX_CREDENTIAL_BYTES = 4096
_APP_CACHE_SENTINEL = "_stonks_authentication_cache_sentinel"
_REQUEST_CACHE_SENTINEL = "stonks_authentication_cache_sentinel"
_REQUEST_CACHE_PRINCIPAL = "stonks_verified_principal"


@dataclass(frozen=True, slots=True)
class _Window:
    identifier: int
    expires_at_micros: int
    count: int
    limit: int
    window_seconds: int


class InMemoryRateLimitStore:
    """Atomic bounded fixed-window store with amortized expiry cleanup."""

    def __init__(self, *, max_keys: int = 10_000) -> None:
        if isinstance(max_keys, bool) or not 1 <= max_keys <= 1_000_000:
            raise ValueError("max_keys must be between 1 and 1000000")
        self._max_keys = max_keys
        self._windows: dict[str, _Window] = {}
        self._expirations: list[tuple[int, str, int]] = []
        self._latest_micros: int | None = None
        self._lock = Lock()

    def consume(
        self,
        key: str,
        *,
        now: datetime,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        _validate_request(key, now, limit, window_seconds)
        micros = _utc_micros(now)
        window_micros = window_seconds * 1_000_000
        identifier = micros // window_micros
        expires_at = (identifier + 1) * window_micros
        retry_after = _seconds_until(expires_at, micros)
        with self._lock:
            if self._latest_micros is not None and micros < self._latest_micros:
                return _denied(retry_after)
            self._latest_micros = micros
            self._expire_windows(micros)
            current = self._windows.get(key)
            if current is not None and (
                current.limit != limit or current.window_seconds != window_seconds
            ):
                return _denied(_seconds_until(current.expires_at_micros, micros))
            if current is None:
                return self._create_window(
                    key,
                    identifier=identifier,
                    expires_at=expires_at,
                    limit=limit,
                    window_seconds=window_seconds,
                    retry_after=retry_after,
                )
            updated = _Window(
                identifier=current.identifier,
                expires_at_micros=current.expires_at_micros,
                count=current.count + 1,
                limit=current.limit,
                window_seconds=current.window_seconds,
            )
            self._windows[key] = updated
            if updated.count > limit:
                return _denied(retry_after)
            return _allowed(limit - updated.count)

    def _create_window(
        self,
        key: str,
        *,
        identifier: int,
        expires_at: int,
        limit: int,
        window_seconds: int,
        retry_after: int,
    ) -> RateLimitDecision:
        if len(self._windows) >= self._max_keys:
            return _denied(retry_after)
        window = _Window(
            identifier=identifier,
            expires_at_micros=expires_at,
            count=1,
            limit=limit,
            window_seconds=window_seconds,
        )
        self._windows[key] = window
        heapq.heappush(self._expirations, (expires_at, key, identifier))
        return _allowed(limit - 1)

    def _expire_windows(self, now_micros: int) -> None:
        while self._expirations and self._expirations[0][0] <= now_micros:
            expires_at, key, identifier = heapq.heappop(self._expirations)
            current = self._windows.get(key)
            if (
                current is not None
                and current.identifier == identifier
                and current.expires_at_micros == expires_at
            ):
                del self._windows[key]


class ApiRateLimitFailure(Exception):
    """Safe failure emitted by the central API limiter."""

    __slots__ = ("error", "retry_after_seconds")

    def __init__(
        self,
        error: StructuredError,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.error = error
        self.retry_after_seconds = retry_after_seconds
        super().__init__(error.message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.error.code.value!r})"


@dataclass(frozen=True, slots=True)
class ApiRateLimitBoundary:
    store: RateLimitStore
    clock: Clock
    principal_requests: int
    direct_peer_edge_requests: int
    window_seconds: int

    def enforce_admission(self, scope: Scope) -> None:
        now = self._now()
        self._consume(
            _edge_peer_key(scope),
            now=now,
            limit=self.direct_peer_edge_requests,
        )
        self._consume(
            _admission_key(scope),
            now=now,
            limit=self.principal_requests,
        )

    def enforce_principal(self, principal: LocalPrincipal) -> None:
        self._consume(
            _principal_key(principal),
            now=self._now(),
            limit=self.principal_requests,
        )

    def _now(self) -> datetime:
        try:
            return self.clock()
        except Exception:
            raise _unavailable() from None

    def _consume(self, key: str, *, now: datetime, limit: int) -> None:
        try:
            decision = self.store.consume(
                key,
                now=now,
                limit=limit,
                window_seconds=self.window_seconds,
            )
        except Exception:
            raise _unavailable() from None
        if not decision.allowed:
            raise ApiRateLimitFailure(
                StructuredError(
                    code=ErrorCode.RATE_LIMITED,
                    message="API rate limit exceeded",
                ),
                retry_after_seconds=decision.retry_after_seconds,
            )


class ApiAdmissionRateLimitMiddleware:
    """Cheap pre-body edge and credential/direct-peer admission."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        limiter = _limiter_from_scope(scope)
        if isinstance(limiter, ApiRateLimitFailure):
            await _send_failure(limiter, scope, receive, send)
            return
        try:
            limiter.enforce_admission(scope)
        except ApiRateLimitFailure as failure:
            await _send_failure(failure, scope, receive, send)
            return
        await self._app(scope, receive, send)


class ApiPrincipalRateLimitMiddleware:
    """Authenticate after body bounds, cache identity, then limit principal."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        limiter = _limiter_from_scope(scope)
        if isinstance(limiter, ApiRateLimitFailure):
            await _send_failure(limiter, scope, receive, send)
            return
        principal = _authenticate_for_key(scope)
        try:
            if principal is not None:
                limiter.enforce_principal(principal)
        except ApiRateLimitFailure as failure:
            await _send_failure(failure, scope, receive, send)
            return
        await self._app(scope, receive, send)


def install_rate_limiter(
    app: FastAPI,
    *,
    store: RateLimitStore,
    clock: Clock,
    limit: int,
    direct_peer_edge_limit: int,
    window_seconds: int,
) -> None:
    if not isinstance(store, RateLimitStore):
        raise TypeError("rate_limit_store must implement RateLimitStore")
    app.state.stonks_rate_limiter = ApiRateLimitBoundary(
        store=store,
        clock=clock,
        principal_requests=limit,
        direct_peer_edge_requests=direct_peer_edge_limit,
        window_seconds=window_seconds,
    )
    setattr(app.state, _APP_CACHE_SENTINEL, object())
    app.add_exception_handler(ApiRateLimitFailure, _rate_limit_failure)
    app.add_middleware(ApiPrincipalRateLimitMiddleware)


def install_admission_rate_limiter(app: FastAPI) -> None:
    app.add_middleware(ApiAdmissionRateLimitMiddleware)


def _limiter_from_scope(scope: Scope) -> ApiRateLimitBoundary | ApiRateLimitFailure:
    app = scope.get("app")
    limiter = getattr(getattr(app, "state", None), "stonks_rate_limiter", None)
    if not isinstance(limiter, ApiRateLimitBoundary):
        return ApiRateLimitFailure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="API rate limit service is unavailable",
            )
        )
    return limiter


async def _rate_limit_failure(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    del request
    if not isinstance(exception, ApiRateLimitFailure):
        exception = ApiRateLimitFailure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="API rate limit service is unavailable",
            )
        )
    envelope = error_envelope(exception.error)
    headers = (
        {"Retry-After": str(exception.retry_after_seconds)}
        if exception.retry_after_seconds is not None
        else None
    )
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


async def _send_failure(
    failure: ApiRateLimitFailure,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    response = await _rate_limit_failure(Request(scope, receive), failure)
    await response(scope, receive, send)


def _authenticate_for_key(scope: Scope) -> LocalPrincipal | None:
    state = scope.setdefault("state", {})
    app = scope.get("app")
    sentinel = getattr(getattr(app, "state", None), _APP_CACHE_SENTINEL, None)
    state[_REQUEST_CACHE_SENTINEL] = sentinel
    state[_REQUEST_CACHE_PRINCIPAL] = None
    authenticator = getattr(getattr(app, "state", None), "stonks_authenticator", None)
    if not isinstance(authenticator, Authenticator):
        return None
    try:
        incoming = AuthenticationRequest(
            authorization=_authorization_header(scope),
            client_host=_direct_peer(scope),
        )
        result = authenticator.authenticate(incoming)
    except (ValidationError, ValueError, TypeError):
        return None
    if isinstance(result, Failure):
        return None
    principal = getattr(result, "value", None)
    if not isinstance(principal, LocalPrincipal):
        return None
    state[_REQUEST_CACHE_PRINCIPAL] = principal
    return principal


def cached_authentication(
    request: Request,
) -> tuple[bool, LocalPrincipal | None]:
    """Return only cache state created by this app's limiter instance."""

    expected = getattr(request.app.state, _APP_CACHE_SENTINEL, None)
    actual = getattr(request.state, _REQUEST_CACHE_SENTINEL, None)
    if expected is None or actual is not expected:
        return False, None
    principal = getattr(request.state, _REQUEST_CACHE_PRINCIPAL, None)
    return True, principal if isinstance(principal, LocalPrincipal) else None


def _authorization_values(scope: Scope) -> list[bytes]:
    return [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == b"authorization" and isinstance(value, bytes)
    ]


def _authorization_header(scope: Scope) -> str | None:
    values = _authorization_values(scope)
    if not values:
        return None
    if len(values) != 1 or len(values[0]) > _MAX_CREDENTIAL_BYTES:
        return ""
    return values[0].decode("latin-1")


def _admission_key(scope: Scope) -> str:
    values = _authorization_values(scope)
    if len(values) == 1 and 0 < len(values[0]) <= _MAX_CREDENTIAL_BYTES:
        return _hashed_bytes_key("credential-admission", values[0])
    return _hashed_key("direct-peer-main", _direct_peer(scope) or "unknown")


def _principal_key(principal: LocalPrincipal) -> str:
    return _hashed_key("principal", principal.subject)


def _edge_peer_key(scope: Scope) -> str:
    return _hashed_key("direct-peer-edge", _direct_peer(scope) or "unknown")


def _direct_peer(scope: Scope) -> str | None:
    client = scope.get("client")
    if not client or not isinstance(client[0], str):
        return None
    return client[0]


def _hashed_key(namespace: str, value: str) -> str:
    return _hashed_bytes_key(namespace, value.encode("utf-8", errors="replace"))


def _hashed_bytes_key(namespace: str, value: bytes) -> str:
    return f"{namespace}:{hashlib.sha256(value).hexdigest()}"


def _validate_request(
    key: str,
    now: datetime,
    limit: int,
    window_seconds: int,
) -> None:
    if not key or len(key) > _MAX_KEY_LENGTH or any(ord(char) < 32 for char in key):
        raise ValueError("rate-limit key is invalid")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("rate-limit clock must be timezone-aware")
    if isinstance(limit, bool) or not 1 <= limit <= 1_000_000:
        raise ValueError("rate-limit request count is invalid")
    if isinstance(window_seconds, bool) or not 1 <= window_seconds <= 86_400:
        raise ValueError("rate-limit window is invalid")


def _utc_micros(now: datetime) -> int:
    delta = now.astimezone(UTC) - _EPOCH
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    if micros < 0:
        raise ValueError("rate-limit clock precedes the Unix epoch")
    return micros


def _seconds_until(expires_at_micros: int, now_micros: int) -> int:
    remaining = expires_at_micros - now_micros
    return max(1, (remaining + 999_999) // 1_000_000)


def _allowed(remaining: int) -> RateLimitDecision:
    return RateLimitDecision(
        allowed=True,
        remaining=remaining,
        retry_after_seconds=0,
    )


def _denied(retry_after_seconds: int) -> RateLimitDecision:
    return RateLimitDecision(
        allowed=False,
        remaining=0,
        retry_after_seconds=retry_after_seconds,
    )


def _unavailable() -> ApiRateLimitFailure:
    return ApiRateLimitFailure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="API rate limit service is unavailable",
        )
    )
