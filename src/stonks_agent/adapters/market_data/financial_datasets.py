"""Strict, read-only Financial Datasets historical-price adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from threading import Lock
from typing import Annotated, Final, Self

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation

FINANCIAL_DATASETS_ORIGIN: Final = "https://api.financialdatasets.ai"
HISTORICAL_PRICES_ENDPOINT: Final = "/prices"
_HISTORICAL_PRICES_URL: Final = (
    f"{FINANCIAL_DATASETS_ORIGIN}{HISTORICAL_PRICES_ENDPOINT}"
)

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
        "_api_key",
        "_budget_lock",
        "_client",
        "_clock",
        "_max_response_bytes",
        "_request_budget",
        "_requests_used",
        "_timeout",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        api_key: str | None,
        request_budget: int = 100,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 1_048_576,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _validate_limits(request_budget, timeout_seconds, max_response_bytes)
        self._client = client
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._request_budget = request_budget
        self._requests_used = 0
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._clock = clock or _utc_now
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
        if request.market != "US" or request.capability != "prices":
            return _failure(
                ProviderDataState.NOT_SUPPORTED,
                "capability_not_supported",
                observed_at,
            )
        api_key = self._api_key
        if api_key is None:
            return _failure(
                ProviderDataState.CONFIG_MISSING,
                "api_key_missing",
                observed_at,
            )
        query = _parse_query(request)
        if query is None:
            return _failure(
                ProviderDataState.FETCH_FAILED,
                "invalid_request",
                observed_at,
            )
        if not self._consume_budget():
            return _failure(
                ProviderDataState.QUOTA_EXHAUSTED,
                "local_rate_budget_exhausted",
                observed_at,
            )
        response = self._request(query, api_key, observed_at)
        if isinstance(response, ProviderObservation):
            return response
        return _parse_response(response, query, request.as_of, observed_at)

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
        try:
            with self._client.stream(
                "GET",
                _HISTORICAL_PRICES_URL,
                params=query.http_params(),
                headers={"Accept": "application/json", "X-API-KEY": api_key},
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code != httpx.codes.OK:
                    return _http_failure(response.status_code, observed_at)
                return self._bounded_body(response, observed_at)
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

    def _bounded_body(
        self,
        response: httpx.Response,
        observed_at: datetime,
    ) -> bytes | ProviderObservation[FinancialDatasetsPrice]:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                return _failure(
                    ProviderDataState.FETCH_FAILED,
                    "invalid_content_length",
                    observed_at,
                )
            if declared_length < 0 or declared_length > self._max_response_bytes:
                return _failure(
                    ProviderDataState.FETCH_FAILED,
                    "response_too_large",
                    observed_at,
                )
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > self._max_response_bytes:
                return _failure(
                    ProviderDataState.FETCH_FAILED,
                    "response_too_large",
                    observed_at,
                )
            body.extend(chunk)
        return bytes(body)

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
    times = tuple(item.time for item in normalized)
    if len(times) != len(set(times)):
        return _failure(
            ProviderDataState.CONFLICT,
            "duplicate_price_time",
            observed_at,
        )
    if any(item.time > as_of.date() for item in normalized):
        return _failure(
            ProviderDataState.CONFLICT,
            "future_data_returned",
            observed_at,
        )
    if any(
        item.time < query.start_date or item.time > query.end_date
        for item in normalized
    ):
        return _failure(
            ProviderDataState.CONFLICT,
            "response_outside_requested_range",
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


def _utc_now() -> datetime:
    return datetime.now(UTC)
