"""Read-only, allowlisted REST adapter for the optional OpenBB sidecar."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from math import isfinite
from typing import Final, Literal, Self

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.market_data import OHLCBar

OPENBB_ORIGIN: Final = "http://127.0.0.1:6900"
OPENBB_HISTORICAL_ENDPOINT: Final = "/api/v1/equity/price/historical"
OPENBB_PROVIDER: Final = "yfinance"
_ALLOWED_QUERY_FIELDS: Final = frozenset({"symbol", "start_date", "end_date"})


class OpenBBHistoricalRecord(BaseModel):
    """Known OHLCV fields plus additive fields retained from OpenBB."""

    model_config = ConfigDict(extra="allow", frozen=True)

    date: datetime | date
    open: Decimal = Field(allow_inf_nan=False)
    high: Decimal = Field(allow_inf_nan=False)
    low: Decimal = Field(allow_inf_nan=False)
    close: Decimal = Field(allow_inf_nan=False)
    volume: Decimal = Field(ge=0, allow_inf_nan=False)


class OpenBBPrice(BaseModel):
    """Canonical OHLC bar paired with the lossless typed provider record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bar: OHLCBar
    provider_record: OpenBBHistoricalRecord


class OpenBBWarning(BaseModel):
    """Stable warning fields while allowing additive OpenBB warning metadata."""

    model_config = ConfigDict(extra="allow", frozen=True)

    category: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=4096)


class OpenBBResponseMetadata(BaseModel):
    """Provenance retained from the OpenBB response envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=256)
    provider: Literal["yfinance"]
    warnings: tuple[OpenBBWarning, ...]
    extra: dict[str, object]


class OpenBBObservation(ProviderObservation[OpenBBPrice]):
    """Provider observation enriched with typed OpenBB provenance."""

    metadata: OpenBBResponseMetadata | None = None


class _OpenBBEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1, max_length=256)
    results: tuple[OpenBBHistoricalRecord, ...]
    provider: Literal["yfinance"]
    warnings: tuple[OpenBBWarning, ...] | None = None
    extra: dict[str, object] | None = None

    def metadata(self) -> OpenBBResponseMetadata:
        return OpenBBResponseMetadata(
            id=self.id,
            provider=self.provider,
            warnings=self.warnings or (),
            extra=dict(self.extra or {}),
        )


class _OpenBBQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9][A-Z0-9.-]*$",
    )
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self

    def http_params(self) -> dict[str, str]:
        params = {"symbol": self.symbol}
        if self.start_date is not None:
            params["start_date"] = self.start_date.isoformat()
        if self.end_date is not None:
            params["end_date"] = self.end_date.isoformat()
        params["provider"] = OPENBB_PROVIDER
        return params


class OpenBBRestAdapter:
    """Fetch historical prices from one fixed sidecar route and provider."""

    __slots__ = ("_client", "_clock", "_max_response_bytes", "_timeout")

    def __init__(
        self,
        *,
        client: httpx.Client,
        origin: str = OPENBB_ORIGIN,
        endpoint: str = OPENBB_HISTORICAL_ENDPOINT,
        provider: str = OPENBB_PROVIDER,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 1_048_576,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _validate_configuration(origin, endpoint, provider)
        _validate_limits(timeout_seconds, max_response_bytes)
        self._client = client
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._clock = clock or _utc_now

    def fetch(self, request: FetchDataRequest) -> OpenBBObservation:
        observed_at = self._observed_at()
        if request.market != "US" or request.capability != "prices":
            return _failure(
                "openbb_capability_not_supported",
                observed_at,
                state=ProviderDataState.NOT_SUPPORTED,
            )
        if observed_at > request.as_of:
            return _failure(
                "openbb_point_in_time_unproven",
                observed_at,
                state=ProviderDataState.CONFLICT,
            )
        parsed = _parse_query(request)
        if isinstance(parsed, str):
            return _failure(f"openbb_invalid_request:{parsed}", observed_at)
        response = self._request(parsed, observed_at)
        if isinstance(response, OpenBBObservation):
            return response
        return _parse_response(response, parsed, request.as_of, observed_at)

    def _request(
        self,
        query: _OpenBBQuery,
        observed_at: datetime,
    ) -> bytes | OpenBBObservation:
        try:
            with self._client.stream(
                "GET",
                f"{OPENBB_ORIGIN}{OPENBB_HISTORICAL_ENDPOINT}",
                params=query.http_params(),
                headers={"Accept": "application/json"},
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code != httpx.codes.OK:
                    return _http_failure(response.status_code, observed_at)
                content_type = response.headers.get("content-type", "")
                if content_type.split(";", maxsplit=1)[0].strip() != "application/json":
                    return _failure("openbb_invalid_response", observed_at)
                if _invalid_content_length(
                    response.headers.get("content-length"),
                    self._max_response_bytes,
                ):
                    return _failure("openbb_invalid_response", observed_at)
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > self._max_response_bytes:
                        return _failure("openbb_invalid_response", observed_at)
                    body.extend(chunk)
                return bytes(body)
        except httpx.HTTPError:
            return _failure("openbb_unavailable", observed_at)

    def _observed_at(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("adapter clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _parse_query(request: FetchDataRequest) -> _OpenBBQuery | str:
    unknown = request.query.keys() - _ALLOWED_QUERY_FIELDS
    if unknown:
        return "disallowed_query_parameter"
    try:
        query = _OpenBBQuery.model_validate(request.query)
    except ValidationError as error:
        location = error.errors()[0].get("loc", ())
        field = location[0] if location else "request"
        if field == "symbol":
            return "invalid_symbol"
        if field == "start_date":
            return "invalid_start_date"
        if field == "end_date":
            return "invalid_end_date"
        return "invalid_date_range"
    if query.end_date is not None and query.end_date > request.as_of.date():
        return "invalid_end_date"
    return query


def _parse_response(
    body: bytes,
    query: _OpenBBQuery,
    as_of: datetime,
    observed_at: datetime,
) -> OpenBBObservation:
    try:
        envelope = _OpenBBEnvelope.model_validate_json(body)
    except ValidationError:
        return _failure("openbb_invalid_response", observed_at)
    metadata = envelope.metadata()
    normalized = _normalize_results(envelope.results, query, as_of, observed_at)
    if isinstance(normalized, str):
        return _failure(
            normalized,
            observed_at,
            state=ProviderDataState.CONFLICT,
            metadata=metadata,
        )
    state = (
        ProviderDataState.AVAILABLE
        if normalized
        else ProviderDataState.LEGITIMATE_EMPTY
    )
    return OpenBBObservation(
        state=state,
        data=normalized,
        completeness=Decimal("1"),
        observed_at=observed_at,
        metadata=metadata,
    )


def _normalize_results(
    records: tuple[OpenBBHistoricalRecord, ...],
    query: _OpenBBQuery,
    as_of: datetime,
    observed_at: datetime,
) -> tuple[OpenBBPrice, ...] | str:
    normalized: list[OpenBBPrice] = []
    event_times: set[datetime] = set()
    for record in records:
        event_time = _normalize_event_time(record.date)
        if event_time is None:
            return "openbb_conflicting_data"
        if event_time > as_of:
            return "openbb_future_data"
        if event_time in event_times:
            return "openbb_duplicate_time"
        if not _within_requested_range(event_time.date(), query):
            return "openbb_response_outside_requested_range"
        event_times.add(event_time)
        price = _normalize_record(record, event_time, as_of, observed_at)
        if price is None:
            return "openbb_conflicting_data"
        normalized.append(price)
    return tuple(sorted(normalized, key=lambda item: item.bar.timeline.event_time))


def _normalize_record(
    record: OpenBBHistoricalRecord,
    event_time: datetime,
    as_of: datetime,
    observed_at: datetime,
) -> OpenBBPrice | None:
    try:
        timeline = EvidenceTimeline(
            event_time=event_time,
            published_at=None,
            available_at=observed_at,
            observed_at=observed_at,
            as_of=as_of,
            availability_certainty=AvailabilityCertainty.PROVEN,
        )
        bar = OHLCBar(
            timeline=timeline,
            open=record.open,
            high=record.high,
            low=record.low,
            close=record.close,
            volume=record.volume,
        )
    except ValidationError:
        return None
    return OpenBBPrice(bar=bar, provider_record=record)


def _normalize_event_time(value: datetime | date) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    return datetime.combine(value, datetime.min.time(), tzinfo=UTC)


def _within_requested_range(value: date, query: _OpenBBQuery) -> bool:
    if query.start_date is not None and value < query.start_date:
        return False
    return query.end_date is None or value <= query.end_date


def _failure(
    reason: str,
    observed_at: datetime,
    *,
    state: ProviderDataState = ProviderDataState.FETCH_FAILED,
    metadata: OpenBBResponseMetadata | None = None,
) -> OpenBBObservation:
    return OpenBBObservation(
        state=state,
        data=(),
        completeness=Decimal("0"),
        reasons=(reason,),
        observed_at=observed_at,
        metadata=metadata,
    )


def _http_failure(status_code: int, observed_at: datetime) -> OpenBBObservation:
    if status_code in {httpx.codes.PAYMENT_REQUIRED, httpx.codes.TOO_MANY_REQUESTS}:
        state = ProviderDataState.QUOTA_EXHAUSTED
    elif status_code == httpx.codes.NOT_FOUND:
        state = ProviderDataState.NOT_SUPPORTED
    else:
        state = ProviderDataState.FETCH_FAILED
    return _failure(
        f"openbb_http_status:{status_code}",
        observed_at,
        state=state,
    )


def _invalid_content_length(value: str | None, maximum: int) -> bool:
    if value is None:
        return False
    return not value.isascii() or not value.isdecimal() or int(value) > maximum


def _validate_configuration(origin: str, endpoint: str, provider: str) -> None:
    if origin != OPENBB_ORIGIN:
        raise ValueError("OpenBB origin is not allowlisted")
    if endpoint != OPENBB_HISTORICAL_ENDPOINT:
        raise ValueError("OpenBB endpoint is not allowlisted")
    if provider != OPENBB_PROVIDER:
        raise ValueError("OpenBB provider is not allowlisted")


def _validate_limits(timeout_seconds: float, max_response_bytes: int) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 60
    ):
        raise ValueError("timeout_seconds must be within (0, 60]")
    if isinstance(max_response_bytes, bool) or max_response_bytes < 1:
        raise ValueError("max_response_bytes must be a positive integer")


def _utc_now() -> datetime:
    return datetime.now(UTC)
