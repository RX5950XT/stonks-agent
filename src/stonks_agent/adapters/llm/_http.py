"""Fixed-origin, bounded, narrowly retried HTTP transport for LLM providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from time import monotonic, sleep

import httpx

from stonks_agent.adapters.llm._common import RawProviderResponse
from stonks_agent.adapters.market_data._http_response import (
    ResponseBodyError,
    read_bounded_raw,
    response_deadline,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.model_policy import ModelRoute
from stonks_agent.domain.research import StructuredLLMRequest
from stonks_contracts.common import canonical_json

MAX_REQUEST_BYTES = 4_194_304
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def request_json(
    *,
    client: httpx.Client,
    route: ModelRoute,
    request: StructuredLLMRequest,
    payload: dict[str, object],
    headers: Mapping[str, str],
    clock: Callable[[], datetime],
    monotonic_clock: Callable[[], float] = monotonic,
    sleeper: Callable[[float], None] = sleep,
) -> Result[RawProviderResponse]:
    content = canonical_json(payload).encode("utf-8")
    if len(content) > MAX_REQUEST_BYTES:
        return _failure(ErrorCode.PAYLOAD_TOO_LARGE, "Model request is too large")
    for retry in range(route.max_transient_retries + 1):
        now = clock()
        remaining = (request.deadline_at - now).total_seconds()
        if now.tzinfo is None or remaining <= 0:
            return _failure(
                ErrorCode.DEADLINE_EXCEEDED,
                "Model request deadline exceeded",
            )
        timeout_seconds = min(float(route.timeout_seconds), remaining)
        started = monotonic_clock()
        deadline = response_deadline(monotonic_clock, timeout_seconds)
        if deadline is None:
            return _failure(ErrorCode.DEADLINE_EXCEEDED, "Model transport clock failed")
        try:
            with client.stream(
                "POST",
                f"{route.origin}{route.endpoint}",
                content=content,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                    **headers,
                },
                timeout=httpx.Timeout(timeout_seconds),
                follow_redirects=False,
            ) as response:
                if response.status_code != httpx.codes.OK:
                    failure = _http_failure(response.status_code)
                    if (
                        response.status_code in _TRANSIENT_STATUSES
                        and retry < route.max_transient_retries
                    ):
                        _backoff(retry, sleeper, clock, request.deadline_at)
                        continue
                    return failure
                content_type = response.headers.get("content-type", "")
                if content_type.split(";", maxsplit=1)[0].strip() != "application/json":
                    return _failure(
                        ErrorCode.MODEL_OUTPUT_INVALID,
                        "Model provider response is invalid",
                    )
                body = read_bounded_raw(
                    response,
                    max_bytes=route.max_response_bytes,
                    deadline=deadline,
                    clock=monotonic_clock,
                )
                if isinstance(body, ResponseBodyError):
                    return _body_failure(body)
                if not body:
                    return _failure(
                        ErrorCode.MODEL_OUTPUT_INVALID,
                        "Model provider response is invalid",
                    )
                elapsed = max(0, int((monotonic_clock() - started) * 1000))
                return Success(
                    RawProviderResponse(
                        raw_body=body,
                        elapsed_ms=elapsed,
                        created_at=clock(),
                    )
                )
        except httpx.DecodingError:
            return _failure(
                ErrorCode.MODEL_OUTPUT_INVALID,
                "Model provider response is invalid",
            )
        except httpx.HTTPError:
            if retry < route.max_transient_retries:
                _backoff(retry, sleeper, clock, request.deadline_at)
                continue
            return _failure(
                ErrorCode.DATA_UNAVAILABLE,
                "Model provider is unavailable",
            )
    return _failure(ErrorCode.DATA_UNAVAILABLE, "Model provider is unavailable")


def validate_api_key(value: str) -> None:
    if (
        not value
        or len(value) > 4096
        or value.strip() != value
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("Model provider credential is invalid")


def _backoff(
    retry: int,
    sleeper: Callable[[float], None],
    clock: Callable[[], datetime],
    deadline: datetime,
) -> None:
    delay = 0.25 * (2**retry)
    if clock() + timedelta(seconds=delay) < deadline:
        sleeper(delay)


def _http_failure(status: int) -> Failure:
    if status in {401, 403}:
        return _failure(ErrorCode.UNAUTHORIZED, "Model provider rejected credentials")
    if status == 400:
        return _failure(ErrorCode.INVALID_INPUT, "Model provider rejected the request")
    if status == 413:
        return _failure(
            ErrorCode.PAYLOAD_TOO_LARGE, "Model provider rejected request size"
        )
    if status == 429:
        return _failure(ErrorCode.RATE_LIMITED, "Model provider rate limit exceeded")
    return _failure(ErrorCode.DATA_UNAVAILABLE, "Model provider is unavailable")


def _body_failure(error: ResponseBodyError) -> Failure:
    if error is ResponseBodyError.DEADLINE_EXCEEDED:
        return _failure(ErrorCode.DEADLINE_EXCEEDED, "Model response deadline exceeded")
    if error is ResponseBodyError.RESPONSE_TOO_LARGE:
        return _failure(ErrorCode.PAYLOAD_TOO_LARGE, "Model response is too large")
    return _failure(
        ErrorCode.MODEL_OUTPUT_INVALID,
        "Model provider response is invalid",
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
