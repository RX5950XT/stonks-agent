"""Uniform API envelope and fail-safe error mapping."""

from __future__ import annotations

from math import ceil
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, computed_field

from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.domain.redaction import redact, redact_text


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=500)
    total_items: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_pages(self) -> int:
        return ceil(self.total_items / self.page_size)


class ResponseMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pagination: Pagination | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    trace_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{32}$",
    )


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=256)
    details: dict[str, object] = Field(default_factory=dict)


class SuccessEnvelope[T](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    success: Literal[True] = True
    status: int = Field(default=200, ge=200, lt=400)
    data: T
    error: None = None
    metadata: ResponseMetadata = Field(default_factory=ResponseMetadata)


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    success: Literal[False] = False
    status: int = Field(ge=400, le=599)
    data: None = None
    error: ApiError
    metadata: ResponseMetadata = Field(default_factory=ResponseMetadata)


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.CAPABILITY_DENIED: 403,
    ErrorCode.EGRESS_DENIED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.DATA_UNAVAILABLE: 503,
    ErrorCode.CONFIGURATION_INVALID: 500,
    ErrorCode.INTERNAL_ERROR: 500,
}


def success_envelope[T](
    data: T,
    *,
    status: int = 200,
    metadata: ResponseMetadata | None = None,
) -> SuccessEnvelope[T]:
    return SuccessEnvelope[T](
        data=data,
        status=status,
        metadata=metadata or ResponseMetadata(),
    )


def error_envelope(
    error: StructuredError,
    *,
    metadata: ResponseMetadata | None = None,
) -> ErrorEnvelope:
    redacted_details = cast(dict[str, object], redact(dict(error.details)))
    return ErrorEnvelope(
        status=_HTTP_STATUS[error.code],
        error=ApiError(
            code=error.code.value,
            message=redact_text(error.message),
            details=redacted_details,
        ),
        metadata=metadata or ResponseMetadata(),
    )


def unexpected_error_envelope(
    exception: BaseException,
    *,
    metadata: ResponseMetadata | None = None,
) -> ErrorEnvelope:
    del exception
    return ErrorEnvelope(
        status=500,
        error=ApiError(code="internal_error", message="Internal server error"),
        metadata=metadata or ResponseMetadata(),
    )
