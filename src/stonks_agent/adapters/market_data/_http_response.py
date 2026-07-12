"""Fail-closed helpers for bounded raw HTTP response bodies."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from enum import StrEnum
from math import isfinite

import httpx


class ResponseBodyError(StrEnum):
    INVALID_CONTENT_LENGTH = "invalid_content_length"
    UNSUPPORTED_CONTENT_ENCODING = "unsupported_content_encoding"
    RESPONSE_TOO_LARGE = "response_too_large"
    DEADLINE_EXCEEDED = "deadline_exceeded"


def response_deadline(
    clock: Callable[[], float],
    timeout_seconds: float,
) -> float | None:
    started_at = clock()
    if isinstance(started_at, bool) or not isfinite(started_at):
        return None
    return started_at + timeout_seconds


def read_bounded_raw(
    response: httpx.Response,
    *,
    max_bytes: int,
    deadline: float | None,
    clock: Callable[[], float],
) -> bytes | ResponseBodyError:
    encoding = response.headers.get("content-encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        return ResponseBodyError.UNSUPPORTED_CONTENT_ENCODING
    declared_error = _declared_length_error(
        response.headers.get("content-length"),
        max_bytes,
    )
    if declared_error is not None:
        return declared_error
    body = bytearray()
    for chunk in _raw_chunks(response):
        if _deadline_expired(clock, deadline):
            return ResponseBodyError.DEADLINE_EXCEEDED
        if len(chunk) > max_bytes - len(body):
            return ResponseBodyError.RESPONSE_TOO_LARGE
        body.extend(chunk)
    if _deadline_expired(clock, deadline):
        return ResponseBodyError.DEADLINE_EXCEEDED
    return bytes(body)


def _raw_chunks(response: httpx.Response) -> Iterator[bytes]:
    if response.is_stream_consumed:
        yield response.content
        return
    yield from response.iter_raw()


def _declared_length_error(
    value: str | None,
    maximum: int,
) -> ResponseBodyError | None:
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal():
        return ResponseBodyError.INVALID_CONTENT_LENGTH
    limit = str(maximum)
    if len(value) > len(limit):
        return ResponseBodyError.INVALID_CONTENT_LENGTH
    if len(value) == len(limit) and value > limit:
        return ResponseBodyError.RESPONSE_TOO_LARGE
    return None


def _deadline_expired(
    clock: Callable[[], float],
    deadline: float | None,
) -> bool:
    if deadline is None:
        return True
    current = clock()
    return isinstance(current, bool) or not isfinite(current) or current >= deadline
