"""Strict, read-only Financial Datasets historical-price adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Annotated, Final, Literal, Self

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from stonks_agent.adapters.market_data._http_response import (
    ResponseBodyError,
    read_bounded_raw,
    response_deadline,
)
from stonks_agent.adapters.market_data.regional.base import RegionalProviderCapability
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.secrets import SecretAccessRequest, SecretRef
from stonks_agent.ports.secret_provider import SecretProvider

FINANCIAL_DATASETS_ORIGIN: Final = "https://api.financialdatasets.ai"
HISTORICAL_PRICES_ENDPOINT: Final = "/prices"
FINANCIAL_DATASETS_SUPPORT: Final = frozenset(
    {
        RegionalProviderCapability(
            provider="financial_datasets",
            market="US",
            capability="prices",
            endpoint=HISTORICAL_PRICES_ENDPOINT,
        )
    }
)
_HISTORICAL_PRICES_URL: Final = (
    f"{FINANCIAL_DATASETS_ORIGIN}{HISTORICAL_PRICES_ENDPOINT}"
)
_SECRET_PURPOSE: Final = "financial_datasets_api_key"

Ticker = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9][A-Z0-9.-]*$",
    ),
]


class PriceInterval(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class HistoricalPricesQuery(BaseModel):
    """Validated query arguments; callers cannot choose a URL or provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: Ticker
    interval: PriceInterval
    start_date: date
    end_date: date
    scenario: Literal["canonical"] = "canonical"

    @model_validator(mode="before")
    @classmethod
    def normalize_common_symbol(cls, value: object) -> object:
        if not isinstance(value, dict) or "symbol" not in value:
            return value
        if "ticker" in value:
            raise ValueError("ticker and symbol cannot both be provided")
        normalized = dict(value)
        normalized["ticker"] = normalized.pop("symbol")
        return normalized

    @field_validator("interval", mode="before")
    @classmethod
    def normalize_common_interval(cls, value: object) -> object:
        return PriceInterval.DAY.value if value == "1d" else value

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self

    def http_params(self) -> dict[str, str]:
        return {
            "ticker": self.ticker,
            "interval": self.interval.value,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
        }


class _OHLCVPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    open: Decimal = Field(gt=0, allow_inf_nan=False)
    close: Decimal = Field(gt=0, allow_inf_nan=False)
    high: Decimal = Field(gt=0, allow_inf_nan=False)
    low: Decimal = Field(gt=0, allow_inf_nan=False)
    volume: int = Field(ge=0)
    time: date

    @model_validator(mode="after")
    def validate_ohlc(self) -> Self:
        if self.high < self.low:
            raise ValueError("high must not be below low")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must be within low/high")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must be within low/high")
        return self


class _FinancialDatasetsPricePayload(_OHLCVPayload):
    ticker: Ticker | None = None


class _HistoricalPricesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ticker: Ticker | None = None
    prices: tuple[_FinancialDatasetsPricePayload, ...]


class FinancialDatasetsPrice(_OHLCVPayload):
    """Validated provider DTO with an explicit effective ticker."""

    ticker: Ticker


class FinancialDatasetsAdapter:
    """Bounded synchronous adapter implementing the provider fetch protocol."""

    __slots__ = (
        "_budget_lock",
        "_client",
        "_clock",
        "_max_response_bytes",
        "_monotonic_clock",
        "_request_budget",
        "_requests_used",
        "_secret_provider",
        "_secret_ref",
        "_timeout",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        secret_provider: SecretProvider,
        secret_ref: SecretRef,
        request_budget: int = 100,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 1_048_576,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        _validate_limits(request_budget, timeout_seconds, max_response_bytes)
        self._client = client
        self._secret_provider = secret_provider
        self._secret_ref = secret_ref
        self._request_budget = request_budget
        self._requests_used = 0
        self._timeout = httpx.Timeout(timeout_seconds)
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._clock = clock or utc_now
        self._monotonic_clock = monotonic_clock or monotonic
        self._budget_lock = Lock()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(origin={FINANCIAL_DATASETS_ORIGIN!r}, "
            f"remaining_requests={self.remaining_requests})"
        )

    @property
    def remaining_requests(self) -> int:
        with self._budget_lock:
            return max(self._request_budget - self._requests_used, 0)

    def fetch(
        self,
        request: FetchDataRequest,
    ) -> ProviderObservation[FinancialDatasetsPrice]:
        observed_at = self._observed_at()
        if not _supports_request(request):
            return _failure(
                ProviderDataState.NOT_SUPPORTED,
                "capability_not_supported",
                observed_at,
            )
        query = _parse_query(request)
        if query is None:
            return _failure(
                ProviderDataState.FETCH_FAILED,
                "invalid_request",
                observed_at,
            )
        api_key = self._resolve_api_key()
        if isinstance(api_key, Failure):
            state, reason = _secret_observation_failure(api_key.error.code)
            return _failure(
                state,
                reason,
                observed_at,
            )
        if not self._consume_budget():
            return _failure(
                ProviderDataState.QUOTA_EXHAUSTED,
                "local_rate_budget_exhausted",
                observed_at,
            )
        response = self._request(query, api_key.value, observed_at)
        if isinstance(response, ProviderObservation):
            return response
        return _parse_response(response, query, request.as_of, observed_at)

    def _resolve_api_key(self) -> Result[str]:
        try:
            resolved = self._secret_provider.resolve(
                SecretAccessRequest(
                    reference=self._secret_ref,
                    purpose=_SECRET_PURPOSE,
                )
            )
            if isinstance(resolved, Failure):
                return _secret_failure(resolved.error.code)
            value = resolved.value.reveal()
            if not _is_valid_api_key(value):
                return _secret_failure(ErrorCode.CONFIGURATION_INVALID)
            return Success(value)
        except Exception:
            return _secret_failure(ErrorCode.INTERNAL_ERROR)

    def _consume_budget(self) -> bool:
        with self._budget_lock:
            if self._requests_used >= self._request_budget:
                return False
            self._requests_used += 1
            return True

    def _request(
        self,
        query: HistoricalPricesQuery,
        api_key: str,
        observed_at: datetime,
    ) -> bytes | ProviderObservation[FinancialDatasetsPrice]:
        deadline = response_deadline(
            self._monotonic_clock,
            self._timeout_seconds,
        )
        if deadline is None:
            return _failure(ProviderDataState.FETCH_FAILED, "timeout", observed_at)
        try:
            with self._client.stream(
                "GET",
                _HISTORICAL_PRICES_URL,
                params=query.http_params(),
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "X-API-KEY": api_key,
                },
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code != httpx.codes.OK:
                    return _http_failure(response.status_code, observed_at)
                body = read_bounded_raw(
                    response,
                    max_bytes=self._max_response_bytes,
                    deadline=deadline,
                    clock=self._monotonic_clock,
                )
                if isinstance(body, ResponseBodyError):
                    return _response_body_failure(body, observed_at)
                return body
        except httpx.TimeoutException:
            return _failure(
                ProviderDataState.FETCH_FAILED,
                "timeout",
                observed_at,
            )
        except httpx.HTTPError:
            return _failure(
                ProviderDataState.FETCH_FAILED,
                "transport_error",
                observed_at,
            )

    def _observed_at(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("adapter clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _parse_query(request: FetchDataRequest) -> HistoricalPricesQuery | None:
    try:
        query = HistoricalPricesQuery.model_validate(request.query)
    except ValidationError:
        return None
    if query.end_date > request.as_of.date():
        return None
    return query


def _is_valid_api_key(value: str) -> bool:
    return (
        1 <= len(value) <= 4096
        and value.strip() == value
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _secret_failure(source_code: ErrorCode) -> Failure:
    safe_code = (
        source_code
        if source_code in {ErrorCode.CONFIGURATION_INVALID, ErrorCode.DATA_UNAVAILABLE}
        else ErrorCode.INTERNAL_ERROR
    )
    return Failure(
        StructuredError(
            code=safe_code,
            message="Financial data credential is unavailable",
        )
    )


def _secret_observation_failure(
    code: ErrorCode,
) -> tuple[ProviderDataState, str]:
    if code is ErrorCode.CONFIGURATION_INVALID:
        return ProviderDataState.CONFIG_MISSING, "api_key_unavailable"
    if code is ErrorCode.DATA_UNAVAILABLE:
        return ProviderDataState.FETCH_FAILED, "secret_provider_unavailable"
    return ProviderDataState.FETCH_FAILED, "secret_provider_failure"


def _response_body_failure(
    error: ResponseBodyError,
    observed_at: datetime,
) -> ProviderObservation[FinancialDatasetsPrice]:
    reason = {
        ResponseBodyError.INVALID_CONTENT_LENGTH: "invalid_content_length",
        ResponseBodyError.UNSUPPORTED_CONTENT_ENCODING: (
            "unsupported_content_encoding"
        ),
        ResponseBodyError.RESPONSE_TOO_LARGE: "response_too_large",
        ResponseBodyError.DEADLINE_EXCEEDED: "timeout",
    }[error]
    return _failure(ProviderDataState.FETCH_FAILED, reason, observed_at)


def _parse_response(
    body: bytes,
    query: HistoricalPricesQuery,
    as_of: datetime,
    observed_at: datetime,
) -> ProviderObservation[FinancialDatasetsPrice]:
    try:
        response = _HistoricalPricesResponse.model_validate_json(body, strict=True)
    except ValidationError:
        return _failure(
            ProviderDataState.FETCH_FAILED,
            "response_schema_invalid",
            observed_at,
        )
    if response.ticker is not None and response.ticker != query.ticker:
        return _failure(
            ProviderDataState.CONFLICT,
            "ticker_mismatch",
            observed_at,
        )
    normalized = _normalize_prices(response, query)
    if normalized is None:
        return _failure(
            ProviderDataState.CONFLICT,
            "ticker_mismatch",
            observed_at,
        )
    normalized_error = _normalized_prices_error(normalized, query, as_of)
    if normalized_error is not None:
        return _failure(
            ProviderDataState.CONFLICT,
            normalized_error,
            observed_at,
        )
    if not normalized:
        return ProviderObservation[FinancialDatasetsPrice](
            state=ProviderDataState.LEGITIMATE_EMPTY,
            data=(),
            completeness=Decimal("1"),
            observed_at=observed_at,
        )
    return ProviderObservation[FinancialDatasetsPrice](
        state=ProviderDataState.AVAILABLE,
        data=normalized,
        completeness=Decimal("1"),
        observed_at=observed_at,
    )


def _normalized_prices_error(
    normalized: tuple[FinancialDatasetsPrice, ...],
    query: HistoricalPricesQuery,
    as_of: datetime,
) -> str | None:
    times = tuple(item.time for item in normalized)
    if len(times) != len(set(times)):
        return "duplicate_price_time"
    if any(item.time > as_of.date() for item in normalized):
        return "future_data_returned"
    if any(
        item.time < query.start_date or item.time > query.end_date
        for item in normalized
    ):
        return "response_outside_requested_range"
    return None


def _normalize_prices(
    response: _HistoricalPricesResponse,
    query: HistoricalPricesQuery,
) -> tuple[FinancialDatasetsPrice, ...] | None:
    prices: list[FinancialDatasetsPrice] = []
    for item in response.prices:
        if item.ticker is not None and item.ticker != query.ticker:
            return None
        prices.append(
            FinancialDatasetsPrice(
                ticker=query.ticker,
                open=item.open,
                close=item.close,
                high=item.high,
                low=item.low,
                volume=item.volume,
                time=item.time,
            )
        )
    return tuple(sorted(prices, key=lambda item: item.time))


def _http_failure(
    status_code: int,
    observed_at: datetime,
) -> ProviderObservation[FinancialDatasetsPrice]:
    if status_code in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
        return _failure(
            ProviderDataState.CONFIG_MISSING,
            "provider_auth_failed",
            observed_at,
        )
    if status_code in {httpx.codes.PAYMENT_REQUIRED, httpx.codes.TOO_MANY_REQUESTS}:
        return _failure(
            ProviderDataState.QUOTA_EXHAUSTED,
            "provider_quota_exhausted",
            observed_at,
        )
    if status_code == httpx.codes.NOT_FOUND:
        return _failure(
            ProviderDataState.NOT_SUPPORTED,
            "ticker_not_supported",
            observed_at,
        )
    return _failure(
        ProviderDataState.FETCH_FAILED,
        f"http_status_{status_code}",
        observed_at,
    )


def _failure(
    state: ProviderDataState,
    reason: str,
    observed_at: datetime,
) -> ProviderObservation[FinancialDatasetsPrice]:
    return ProviderObservation[FinancialDatasetsPrice](
        state=state,
        data=(),
        completeness=Decimal("0"),
        reasons=(reason,),
        observed_at=observed_at,
    )


def _validate_limits(
    request_budget: int,
    timeout_seconds: float,
    max_response_bytes: int,
) -> None:
    if (
        isinstance(request_budget, bool)
        or not isinstance(request_budget, int)
        or request_budget < 0
    ):
        raise ValueError("request_budget must be a non-negative integer")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > 60
    ):
        raise ValueError("timeout_seconds must be within (0, 60]")
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or max_response_bytes < 1
    ):
        raise ValueError("max_response_bytes must be a positive integer")


def _supports_request(request: FetchDataRequest) -> bool:
    return any(
        declaration.market == request.market
        and declaration.capability == request.capability
        for declaration in FINANCIAL_DATASETS_SUPPORT
    )
