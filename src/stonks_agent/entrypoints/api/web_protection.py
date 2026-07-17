"""Central browser-facing security headers, CSRF, and safe exception boundaries."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import AddressValueError, IPv6Address
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.entrypoints.api.envelope import ErrorEnvelope, error_envelope

AUTH_COOKIE_NAME = "__Host-stonks_session"
CSRF_COOKIE_NAME = "__Host-stonks_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'; script-src 'none'; "
    "style-src 'self'"
)

_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$")
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
_CSRF_VALUE = re.compile(r"^[A-Za-z0-9._~-]+$")
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ORIGIN_OPTIONAL_METHODS = frozenset({"GET", "HEAD"})
_MAX_COOKIE_HEADER_BYTES = 8192
_INSTALLED_STATE_KEY = "_stonks_web_protection_policy"
_SECURITY_HEADERS = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), geolocation=(), microphone=()"),
)


@dataclass(frozen=True, slots=True)
class CookieAuthPolicy:
    """Explicit browser-cookie authentication policy with one canonical origin."""

    public_origin: str
    auth_cookie_name: str = AUTH_COOKIE_NAME
    csrf_cookie_name: str = CSRF_COOKIE_NAME
    csrf_header_name: str = CSRF_HEADER_NAME

    def __post_init__(self) -> None:
        if not _is_canonical_https_origin(self.public_origin):
            raise ValueError("public_origin must be one canonical HTTPS origin")
        if not _is_host_cookie_name(self.auth_cookie_name):
            raise ValueError("auth cookie must use a bounded __Host- name")
        if not _is_host_cookie_name(self.csrf_cookie_name):
            raise ValueError("CSRF cookie must use a bounded __Host- name")
        if self.auth_cookie_name == self.csrf_cookie_name:
            raise ValueError("auth and CSRF cookie names must differ")
        if not _COOKIE_NAME.fullmatch(
            self.csrf_header_name
        ) or self.csrf_header_name.lower() in {"authorization", "cookie"}:
            raise ValueError("CSRF header name is invalid")


def install_web_protection(
    app: FastAPI,
    *,
    cookie_auth: CookieAuthPolicy | None = None,
    boundary_installer: Callable[[FastAPI], None] | None = None,
) -> None:
    """Install one idempotent browser and exception security boundary."""

    signature = (cookie_auth, boundary_installer)
    installed = getattr(app.state, _INSTALLED_STATE_KEY, None)
    if installed is not None:
        if installed != signature:
            raise ValueError("web protection is already installed with another policy")
        return
    setattr(app.state, _INSTALLED_STATE_KEY, signature)
    app.add_exception_handler(RequestValidationError, _request_validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected_error)
    app.add_middleware(_CookieAuthenticationMiddleware, policy=cookie_auth)
    if boundary_installer is not None:
        boundary_installer(app)
    app.add_middleware(_ExceptionBoundaryMiddleware)
    app.add_middleware(_SecurityHeadersMiddleware)


class _SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return
            protected = {name for name, _ in _SECURITY_HEADERS}
            headers = [
                (name, value)
                for name, value in message.get("headers", [])
                if name.lower() not in protected
            ]
            updated = dict(message)
            updated["headers"] = [*headers, *_SECURITY_HEADERS]
            await send(updated)

        await self._app(scope, receive, send_with_security_headers)


class _ExceptionBoundaryMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        response_started = False

        async def track_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, receive, track_response)
        except Exception as error:
            if response_started:
                raise
            response = _unexpected_response(error)
            await response(scope, receive, send)


class _CookieAuthenticationMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        policy: CookieAuthPolicy | None,
    ) -> None:
        self._app = app
        self._policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        parsed = _parse_cookies(scope)
        if parsed is None:
            await _send_error(scope, receive, send, _invalid_request())
            return
        auth_cookie_name = (
            self._policy.auth_cookie_name if self._policy else AUTH_COOKIE_NAME
        )
        credential = parsed.get(auth_cookie_name)
        if credential is None:
            await self._app(scope, receive, send)
            return
        if self._policy is None:
            await _send_error(scope, receive, send, _unauthorized())
            return
        rejection = _validate_cookie_request(scope, parsed, self._policy, credential)
        if rejection is not None:
            await _send_error(scope, receive, send, rejection)
            return
        authorized_scope = dict(scope)
        authorized_scope["headers"] = [
            *_headers_without_protected_cookies(scope, parsed, self._policy),
            (b"authorization", f"Bearer {credential}".encode("ascii")),
        ]
        await self._app(authorized_scope, receive, send)


def _validate_cookie_request(
    scope: Scope,
    cookies: dict[str, str],
    policy: CookieAuthPolicy,
    credential: str,
) -> StructuredError | None:
    if _header_values(scope, b"authorization"):
        return _invalid_request()
    if not _valid_credential(credential):
        return _unauthorized()
    if _target_origin(scope) != policy.public_origin:
        return _forbidden()
    method = scope["method"].upper()
    origins = _header_values(scope, b"origin")
    if origins and (
        len(origins) != 1 or _decode_ascii(origins[0]) != policy.public_origin
    ):
        return _forbidden()
    if not origins and method not in _ORIGIN_OPTIONAL_METHODS:
        return _forbidden()
    if method in _CSRF_SAFE_METHODS:
        return None
    cookie_token = cookies.get(policy.csrf_cookie_name)
    header_tokens = _header_values(
        scope, policy.csrf_header_name.lower().encode("ascii")
    )
    if (
        cookie_token is None
        or len(header_tokens) != 1
        or not _valid_csrf_token(cookie_token)
    ):
        return _forbidden()
    header_token = _decode_ascii(header_tokens[0])
    if header_token is None or not _valid_csrf_token(header_token):
        return _forbidden()
    if not hmac.compare_digest(cookie_token, header_token):
        return _forbidden()
    return None


def _parse_cookies(scope: Scope) -> dict[str, str] | None:
    raw_values = _header_values(scope, b"cookie")
    if not raw_values:
        return {}
    if len(raw_values) != 1 or len(raw_values[0]) > _MAX_COOKIE_HEADER_BYTES:
        return None
    rendered = _decode_ascii(raw_values[0])
    if rendered is None:
        return None
    cookies: dict[str, str] = {}
    for part in rendered.split(";"):
        item = part.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        if (
            not separator
            or not _COOKIE_NAME.fullmatch(name)
            or not _COOKIE_VALUE.fullmatch(value)
            or name in cookies
        ):
            return None
        cookies[name] = value
    return cookies


def _target_origin(scope: Scope) -> str | None:
    hosts = _header_values(scope, b"host")
    if len(hosts) != 1:
        return None
    host = _decode_ascii(hosts[0])
    scheme = scope.get("scheme", "").lower()
    if (
        host is None
        or scheme != "https"
        or not host
        or any(char.isspace() for char in host)
    ):
        return None
    return f"{scheme}://{host.lower()}"


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope["headers"] if key.lower() == name]


def _headers_without_protected_cookies(
    scope: Scope,
    cookies: dict[str, str],
    policy: CookieAuthPolicy,
) -> list[tuple[bytes, bytes]]:
    headers = [
        (name, value) for name, value in scope["headers"] if name.lower() != b"cookie"
    ]
    remaining = {
        name: value
        for name, value in cookies.items()
        if name not in {policy.auth_cookie_name, policy.csrf_cookie_name}
    }
    if remaining:
        rendered = "; ".join(f"{name}={value}" for name, value in remaining.items())
        headers.append((b"cookie", rendered.encode("ascii")))
    return headers


def _decode_ascii(value: bytes) -> str | None:
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        return None


def _valid_credential(value: str) -> bool:
    return 32 <= len(value) <= 4096 and _COOKIE_VALUE.fullmatch(value) is not None


def _valid_csrf_token(value: str) -> bool:
    return 32 <= len(value) <= 256 and _CSRF_VALUE.fullmatch(value) is not None


def _is_host_cookie_name(value: str) -> bool:
    return (
        value.startswith("__Host-")
        and len(value) <= 128
        and _COOKIE_NAME.fullmatch(value) is not None
    )


def _is_canonical_https_origin(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.isascii()
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.endswith(".")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    if port is not None and (port == 0 or port == 443):
        return False
    rendered_host = parsed.hostname.lower()
    if ":" in rendered_host:
        try:
            rendered_host = f"[{IPv6Address(rendered_host).compressed}]"
        except AddressValueError:
            return False
    rendered_port = "" if port is None else f":{port}"
    canonical = f"https://{rendered_host}{rendered_port}"
    return value == canonical


async def _request_validation_error(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    del request, exception
    return _json_error(_invalid_request())


async def _http_error(request: Request, exception: Exception) -> JSONResponse:
    del request
    if not isinstance(exception, HTTPException):
        return _unexpected_response(exception)
    return _safe_http_response(exception.status_code)


async def _unexpected_error(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    del request
    return _unexpected_response(exception)


def _safe_http_response(status: int) -> JSONResponse:
    if not 400 <= status <= 599:
        return _unexpected_response(ValueError("invalid HTTP error status"))
    code, message = _http_error_shape(status)
    envelope = error_envelope(StructuredError(code=code, message=message))
    if envelope.status != status:
        envelope = envelope.model_copy(update={"status": status})
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return _envelope_response(envelope, headers=headers)


def _http_error_shape(status: int) -> tuple[ErrorCode, str]:
    shapes = {
        400: (ErrorCode.INVALID_INPUT, "Request is invalid"),
        401: (ErrorCode.UNAUTHORIZED, "Authentication failed"),
        403: (ErrorCode.FORBIDDEN, "Permission denied"),
        404: (ErrorCode.NOT_FOUND, "Resource not found"),
        409: (ErrorCode.CONFLICT, "Request conflict"),
        413: (ErrorCode.PAYLOAD_TOO_LARGE, "Request body is too large"),
        429: (ErrorCode.RATE_LIMITED, "Rate limit exceeded"),
        503: (ErrorCode.DATA_UNAVAILABLE, "Service is unavailable"),
    }
    if status in shapes:
        return shapes[status]
    if status < 500:
        return ErrorCode.INVALID_INPUT, "Request failed"
    return ErrorCode.INTERNAL_ERROR, "Internal server error"


def _unexpected_response(exception: BaseException) -> JSONResponse:
    del exception
    envelope = error_envelope(
        StructuredError(
            code=ErrorCode.INTERNAL_ERROR,
            message="Internal server error",
        )
    )
    return _envelope_response(envelope)


def _json_error(error: StructuredError) -> JSONResponse:
    return _envelope_response(error_envelope(error))


def _envelope_response(
    envelope: ErrorEnvelope,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


async def _send_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    error: StructuredError,
) -> None:
    response = _json_error(error)
    await response(scope, receive, send)


def _invalid_request() -> StructuredError:
    return StructuredError(code=ErrorCode.INVALID_INPUT, message="Request is invalid")


def _unauthorized() -> StructuredError:
    return StructuredError(
        code=ErrorCode.UNAUTHORIZED,
        message="Authentication failed",
    )


def _forbidden() -> StructuredError:
    return StructuredError(
        code=ErrorCode.FORBIDDEN,
        message="Permission denied",
    )
