"""Shared typed failures and origin validation for remote worker HTTP adapters."""

from __future__ import annotations

from urllib.parse import urlsplit

from stonks_agent.adapters.market_data._http_response import ResponseBodyError
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError


def worker_failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))


def invalid_response() -> Failure:
    return worker_failure(ErrorCode.MODEL_OUTPUT_INVALID, "Worker response is invalid")


def status_failure(status: int) -> Failure:
    if status in {401, 403}:
        return worker_failure(ErrorCode.UNAUTHORIZED, "Worker rejected the request")
    if status == 408:
        return worker_failure(ErrorCode.DEADLINE_EXCEEDED, "Worker deadline exceeded")
    if status == 413:
        return worker_failure(
            ErrorCode.PAYLOAD_TOO_LARGE, "Worker rejected request size"
        )
    if status in {400, 409, 422}:
        return worker_failure(ErrorCode.INVALID_INPUT, "Worker rejected the request")
    if status == 429:
        return worker_failure(ErrorCode.RATE_LIMITED, "Worker rate limit exceeded")
    return worker_failure(ErrorCode.DATA_UNAVAILABLE, "Worker is unavailable")


def body_failure(error: ResponseBodyError) -> Failure:
    if error is ResponseBodyError.DEADLINE_EXCEEDED:
        return worker_failure(
            ErrorCode.DEADLINE_EXCEEDED, "Worker response deadline exceeded"
        )
    if error is ResponseBodyError.RESPONSE_TOO_LARGE:
        return worker_failure(
            ErrorCode.PAYLOAD_TOO_LARGE, "Worker response is too large"
        )
    return invalid_response()


def valid_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )
