"""Bounded pre-authentication admission control for isolated services."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import time
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Any

type Message = MutableMapping[str, Any]
type Scope = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
type MonotonicClock = Callable[[], float]

_MAX_CREDENTIAL_BYTES = 4_096
_MAX_HEADERS = 128
_MAX_HEADER_BYTES = 65_536
_MAX_KEY_LENGTH = 128
_RESERVED_RESPONSE_HEADERS = frozenset(
    {
        b"content-length",
        b"content-type",
        b"cache-control",
        b"x-content-type-options",
        b"retry-after",
    }
)


class ServiceAdmissionResponseStyle(StrEnum):
    """Supported safe rejection formats."""

    ENVELOPE = "envelope"
    OPENBB = "openbb"


@dataclass(frozen=True, slots=True)
class ServiceAdmissionPolicy:
    """Immutable limits for one isolated service process."""

    direct_peer_requests: int = 240
    credential_requests: int = 120
    window_seconds: int = 60
    max_keys: int = 4_096

    def __post_init__(self) -> None:
        _validate_integer(
            self.direct_peer_requests,
            name="direct_peer_requests",
            maximum=1_000_000,
        )
        _validate_integer(
            self.credential_requests,
            name="credential_requests",
            maximum=1_000_000,
        )
        _validate_integer(
            self.window_seconds,
            name="window_seconds",
            maximum=86_400,
        )
        _validate_integer(self.max_keys, name="max_keys", maximum=1_000_000)


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


@dataclass(frozen=True, slots=True)
class _Window:
    identifier: int
    expires_at: float
    count: int
    limit: int
    window_seconds: int


class FixedWindowAdmissionStore:
    """Atomic fixed-window counters with bounded key cardinality."""

    def __init__(self, *, max_keys: int = 4_096) -> None:
        _validate_integer(max_keys, name="max_keys", maximum=1_000_000)
        self._max_keys = max_keys
        self._windows: dict[str, _Window] = {}
        self._expirations: list[tuple[float, str, int]] = []
        self._latest: float | None = None
        self._lock = Lock()

    @property
    def active_keys(self) -> tuple[str, ...]:
        """Expose only hashed namespaces for bounded diagnostics and tests."""

        with self._lock:
            return tuple(sorted(self._windows))

    def consume(
        self,
        key: str,
        *,
        now: float,
        limit: int,
        window_seconds: int,
    ) -> AdmissionDecision:
        _validate_consume(key, now, limit, window_seconds)
        identifier = int(now // window_seconds)
        expires_at = float((identifier + 1) * window_seconds)
        retry_after = _retry_after(expires_at, now)
        with self._lock:
            if self._latest is not None and now < self._latest:
                return _denied(retry_after)
            self._latest = now
            self._expire(now)
            current = self._windows.get(key)
            if current is None:
                if len(self._windows) >= self._max_keys:
                    return _denied(retry_after)
                return self._create(
                    key,
                    identifier=identifier,
                    expires_at=expires_at,
                    limit=limit,
                    window_seconds=window_seconds,
                    retry_after=retry_after,
                )
            if current.limit != limit or current.window_seconds != window_seconds:
                return _denied(_retry_after(current.expires_at, now))
            updated = _Window(
                identifier=current.identifier,
                expires_at=current.expires_at,
                count=current.count + 1,
                limit=current.limit,
                window_seconds=current.window_seconds,
            )
            self._windows[key] = updated
            if updated.count > limit:
                return _denied(_retry_after(updated.expires_at, now))
            return _allowed(limit - updated.count)

    def _create(
        self,
        key: str,
        *,
        identifier: int,
        expires_at: float,
        limit: int,
        window_seconds: int,
        retry_after: int,
    ) -> AdmissionDecision:
        self._windows[key] = _Window(
            identifier=identifier,
            expires_at=expires_at,
            count=1,
            limit=limit,
            window_seconds=window_seconds,
        )
        heapq.heappush(self._expirations, (expires_at, key, identifier))
        return _allowed(limit - 1)

    def _expire(self, now: float) -> None:
        while self._expirations and self._expirations[0][0] <= now:
            expires_at, key, identifier = heapq.heappop(self._expirations)
            current = self._windows.get(key)
            if (
                current is not None
                and current.identifier == identifier
                and current.expires_at == expires_at
            ):
                del self._windows[key]


class ServiceAdmissionMiddleware:
    """Reject abusive or spoofed requests before JWT verification."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        policy: ServiceAdmissionPolicy | None = None,
        clock: MonotonicClock = time.monotonic,
        store: FixedWindowAdmissionStore | None = None,
        response_style: ServiceAdmissionResponseStyle = (
            ServiceAdmissionResponseStyle.ENVELOPE
        ),
        extra_response_headers: Sequence[tuple[bytes, bytes]] = (),
    ) -> None:
        policy = policy or ServiceAdmissionPolicy()
        self._app = app
        self._policy = policy
        self._clock = clock
        self._store = store or FixedWindowAdmissionStore(max_keys=policy.max_keys)
        self._response_style = response_style
        self._extra_response_headers = _validate_extra_headers(extra_response_headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        peer = _direct_peer(scope)
        now = _safe_now(self._clock)
        if now is None:
            await self._reject(send, status=503, code="service_unavailable")
            return
        edge = self._consume(
            _hashed_key("direct-peer-edge", peer),
            now=now,
            limit=self._policy.direct_peer_requests,
        )
        if edge is None:
            await self._reject(send, status=503, code="service_unavailable")
            return
        if not edge.allowed:
            await self._reject(
                send,
                status=429,
                code="rate_limited",
                retry_after=edge.retry_after_seconds,
            )
            return
        headers = _safe_headers(scope)
        if headers is None or _has_forwarded_identity(headers):
            await self._reject(send, status=400, code="invalid_request")
            return
        admission = self._consume(
            _admission_key(headers, peer),
            now=now,
            limit=self._policy.credential_requests,
        )
        if admission is None:
            await self._reject(send, status=503, code="service_unavailable")
            return
        if not admission.allowed:
            await self._reject(
                send,
                status=429,
                code="rate_limited",
                retry_after=admission.retry_after_seconds,
            )
            return
        await self._app(scope, receive, send)

    def _consume(
        self,
        key: str,
        *,
        now: float,
        limit: int,
    ) -> AdmissionDecision | None:
        try:
            return self._store.consume(
                key,
                now=now,
                limit=limit,
                window_seconds=self._policy.window_seconds,
            )
        except Exception:
            return None

    async def _reject(
        self,
        send: Send,
        *,
        status: int,
        code: str,
        retry_after: int | None = None,
    ) -> None:
        body = _response_body(self._response_style, status=status, code=code)
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            *self._extra_response_headers,
        ]
        if retry_after is not None:
            headers.append((b"retry-after", str(retry_after).encode("ascii")))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})


def _safe_now(clock: MonotonicClock) -> float | None:
    try:
        now = clock()
    except Exception:
        return None
    return now if math.isfinite(now) and now >= 0 else None


def _safe_headers(scope: Scope) -> tuple[tuple[bytes, bytes], ...] | None:
    raw = scope.get("headers", ())
    if not isinstance(raw, (list, tuple)) or len(raw) > _MAX_HEADERS:
        return None
    headers: list[tuple[bytes, bytes]] = []
    total = 0
    for item in raw:
        if not (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
        ):
            return None
        name, value = item
        total += len(name) + len(value)
        if total > _MAX_HEADER_BYTES:
            return None
        headers.append((name.lower(), value))
    return tuple(headers)


def _has_forwarded_identity(headers: tuple[tuple[bytes, bytes], ...]) -> bool:
    return any(
        name == b"forwarded" or name == b"x-real-ip" or name.startswith(b"x-forwarded-")
        for name, _value in headers
    )


def _admission_key(
    headers: tuple[tuple[bytes, bytes], ...],
    peer: str,
) -> str:
    credentials = [value for name, value in headers if name == b"authorization"]
    if len(credentials) == 1 and 0 < len(credentials[0]) <= _MAX_CREDENTIAL_BYTES:
        return _hashed_bytes_key("credential-admission", credentials[0])
    return _hashed_key("direct-peer-main", peer)


def _direct_peer(scope: Scope) -> str:
    client = scope.get("client")
    if not (
        isinstance(client, (list, tuple))
        and len(client) == 2
        and isinstance(client[0], str)
        and 0 < len(client[0]) <= 255
        and not any(ord(character) < 32 for character in client[0])
    ):
        return "unknown"
    return client[0]


def _hashed_key(namespace: str, value: str) -> str:
    return _hashed_bytes_key(namespace, value.encode("utf-8", errors="replace"))


def _hashed_bytes_key(namespace: str, value: bytes) -> str:
    return f"{namespace}:{hashlib.sha256(value).hexdigest()}"


def _response_body(
    style: ServiceAdmissionResponseStyle,
    *,
    status: int,
    code: str,
) -> bytes:
    message = {
        "invalid_request": "Forwarded client identity is not accepted",
        "rate_limited": "Service admission rate limit exceeded",
        "service_unavailable": "Service admission control is unavailable",
    }[code]
    if style is ServiceAdmissionResponseStyle.OPENBB:
        payload: dict[str, object] = {"detail": message}
    else:
        payload = {
            "success": False,
            "status": status,
            "data": None,
            "error": {"code": code, "message": message},
            "metadata": None,
        }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _validate_extra_headers(
    headers: Sequence[tuple[bytes, bytes]],
) -> tuple[tuple[bytes, bytes], ...]:
    validated: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        if not isinstance(name, bytes) or not isinstance(value, bytes) or not name:
            raise ValueError("extra response header is invalid")
        lowered = name.lower()
        if (
            lowered in _RESERVED_RESPONSE_HEADERS
            or any(byte < 32 or byte > 126 for byte in name)
            or any(byte in {10, 13} for byte in value)
        ):
            raise ValueError("extra response header is invalid")
        validated.append((lowered, value))
    return tuple(validated)


def _validate_consume(
    key: str,
    now: float,
    limit: int,
    window_seconds: int,
) -> None:
    if (
        not key
        or len(key) > _MAX_KEY_LENGTH
        or any(ord(character) < 32 for character in key)
    ):
        raise ValueError("admission key is invalid")
    if not math.isfinite(now) or now < 0:
        raise ValueError("admission clock is invalid")
    _validate_integer(limit, name="limit", maximum=1_000_000)
    _validate_integer(window_seconds, name="window_seconds", maximum=86_400)


def _validate_integer(value: int, *, name: str, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{name} is outside the supported range")


def _retry_after(expires_at: float, now: float) -> int:
    return max(1, math.ceil(expires_at - now))


def _allowed(remaining: int) -> AdmissionDecision:
    return AdmissionDecision(
        allowed=True,
        remaining=remaining,
        retry_after_seconds=0,
    )


def _denied(retry_after: int) -> AdmissionDecision:
    return AdmissionDecision(
        allowed=False,
        remaining=0,
        retry_after_seconds=retry_after,
    )
