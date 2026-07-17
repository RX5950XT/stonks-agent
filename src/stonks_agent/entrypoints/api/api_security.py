"""Single typed composition for every FastAPI security boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.entrypoints.api.envelope import error_envelope
from stonks_agent.entrypoints.api.rate_limits import (
    InMemoryRateLimitStore,
    install_admission_rate_limiter,
    install_rate_limiter,
)
from stonks_agent.entrypoints.api.request_limits import (
    ForwardedHeaderRejectMiddleware,
    RequestBodyLimitMiddleware,
)
from stonks_agent.entrypoints.api.web_protection import (
    CookieAuthPolicy,
    install_web_protection,
)
from stonks_agent.ports.rate_limit_store import RateLimitStore

type Clock = Callable[[], datetime]

_CORS_METHODS = ("GET", "POST", "OPTIONS")
_CORS_HEADERS = (
    "Authorization",
    "Content-Type",
    "Last-Event-ID",
    "X-CSRF-Token",
)


class ApiSecurityPolicy(BaseModel):
    """Closed API policy; browser origins must be canonical exact values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_cors_origins: tuple[str, ...] = ()
    rate_limit_requests: int = Field(default=120, ge=1, le=1_000_000)
    direct_peer_edge_requests: int = Field(default=600, ge=1, le=1_000_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=86_400)
    rate_limit_max_keys: int = Field(default=10_000, ge=1, le=1_000_000)
    request_body_max_frames: int = Field(default=256, ge=1, le=4096)

    @field_validator("allowed_cors_origins")
    @classmethod
    def validate_exact_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 64 or len(set(values)) != len(values):
            raise ValueError("CORS origins must be unique and bounded")
        for value in values:
            _validate_origin(value)
        return values

    @model_validator(mode="after")
    def validate_edge_limit(self) -> Self:
        if self.direct_peer_edge_requests < self.rate_limit_requests:
            raise ValueError("direct peer edge limit cannot be below principal limit")
        return self


class ExactCORSMiddleware(CORSMiddleware):
    """Starlette CORS with a uniform fail-closed preflight envelope."""

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if response.status_code < 400:
            return response
        envelope = error_envelope(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="CORS preflight request is not allowed",
            )
        )
        preserved = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in {"content-length", "content-type"}
        }
        return JSONResponse(
            status_code=envelope.status,
            content=envelope.model_dump(mode="json"),
            headers=preserved,
        )


@dataclass(frozen=True, slots=True)
class ApiSecurityOptions:
    """Runtime dependencies and policy passed through every app factory."""

    policy: ApiSecurityPolicy = field(default_factory=ApiSecurityPolicy)
    rate_limit_store: RateLimitStore | None = None
    clock: Clock | None = None
    cookie_auth: CookieAuthPolicy | None = None


@dataclass(frozen=True, slots=True)
class _InstalledApiSecurity:
    policy: ApiSecurityPolicy
    max_request_bytes: int
    rate_limit_store: RateLimitStore | None
    clock: Clock | None
    cookie_auth: CookieAuthPolicy | None

    def matches(self, runtime: ApiSecurityOptions, maximum: int) -> bool:
        return (
            self.policy == runtime.policy
            and self.max_request_bytes == maximum
            and self.rate_limit_store is runtime.rate_limit_store
            and self.clock is runtime.clock
            and self.cookie_auth == runtime.cookie_auth
        )


def install_api_security(
    app: FastAPI,
    *,
    max_request_bytes: int,
    options: ApiSecurityOptions | None = None,
) -> None:
    """Install body, rate, CORS, web/error controls in a stable order."""

    runtime = options or ApiSecurityOptions()
    selected = runtime.policy
    existing = getattr(app.state, "stonks_api_security_policy", None)
    if existing is not None:
        if not isinstance(existing, _InstalledApiSecurity) or not existing.matches(
            runtime, max_request_bytes
        ):
            raise ValueError("API security is already configured differently")
        return
    if isinstance(max_request_bytes, bool) or max_request_bytes < 1:
        raise ValueError("max_request_bytes must be a positive integer")
    app.state.stonks_api_security_policy = _InstalledApiSecurity(
        policy=selected,
        max_request_bytes=max_request_bytes,
        rate_limit_store=runtime.rate_limit_store,
        clock=runtime.clock,
        cookie_auth=runtime.cookie_auth,
    )
    install_rate_limiter(
        app,
        store=(
            runtime.rate_limit_store
            if runtime.rate_limit_store is not None
            else InMemoryRateLimitStore(max_keys=selected.rate_limit_max_keys)
        ),
        clock=runtime.clock or _utc_now,
        limit=selected.rate_limit_requests,
        direct_peer_edge_limit=selected.direct_peer_edge_requests,
        window_seconds=selected.rate_limit_window_seconds,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=max_request_bytes,
        max_frames=selected.request_body_max_frames,
    )

    def install_outer_boundaries(selected_app: FastAPI) -> None:
        selected_app.add_middleware(ForwardedHeaderRejectMiddleware)
        if selected.allowed_cors_origins:
            selected_app.add_middleware(
                ExactCORSMiddleware,
                allow_origins=list(selected.allowed_cors_origins),
                allow_credentials=False,
                allow_methods=list(_CORS_METHODS),
                allow_headers=list(_CORS_HEADERS),
                expose_headers=["Retry-After"],
                max_age=600,
            )
        install_admission_rate_limiter(selected_app)

    install_web_protection(
        app,
        cookie_auth=runtime.cookie_auth,
        boundary_installer=install_outer_boundaries,
    )


def _validate_origin(value: str) -> None:
    if not value or len(value) > 256 or not value.isascii() or value == "*":
        raise ValueError("CORS origin is invalid")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("CORS origin port is invalid") from error
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or hostname != hostname.lower()
        or hostname.endswith(".")
        or "%" in hostname
    ):
        raise ValueError("CORS origin must be a canonical origin")
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if parsed.scheme == "http" else 443
    rendered_port = "" if port in {None, default_port} else f":{port}"
    if value != f"{parsed.scheme}://{rendered_host}{rendered_port}":
        raise ValueError("CORS origin must be a canonical origin")
    if parsed.scheme == "http" and hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("non-local CORS origins require HTTPS")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("CORS origin port is invalid")


def _utc_now() -> datetime:
    return datetime.now(UTC)
