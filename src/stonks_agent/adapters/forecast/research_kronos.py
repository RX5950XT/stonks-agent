"""Build one snapshot-bound Kronos forecast for a fenced research lease."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from stonks_agent.adapters.forecast.kronos import (
    KronosHttpAdapter,
    KronosWorkerConfiguration,
    build_kronos_worker_request,
)
from stonks_agent.domain.calendar import ExchangeCalendar
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.research_job import ResearchLeaseInput
from stonks_agent.domain.signal import ForecastOutputArtifact, ForecastRequest
from stonks_contracts.evidence import EvidenceItem, EvidenceKind
from stonks_contracts.kronos import KronosSamplingPolicy, VolumeQuality
from stonks_contracts.market_data import Bar, BarSeries, DataQuality

_MAX_CONTEXT_BARS = 512


class ResearchKronosForecaster:
    """Translate immutable daily evidence before invoking the HTTP adapter."""

    __slots__ = (
        "_adapter",
        "_calendar",
        "_calendar_valid_from",
        "_calendar_valid_through",
        "_clock",
        "_configuration",
        "_horizon_bars",
        "_sampling",
    )

    def __init__(
        self,
        *,
        adapter: KronosHttpAdapter,
        configuration: KronosWorkerConfiguration,
        calendar: ExchangeCalendar,
        sampling: KronosSamplingPolicy,
        horizon_bars: int,
        clock: Callable[[], datetime],
        calendar_valid_from: date,
        calendar_valid_through: date,
    ) -> None:
        if (
            not 1 <= horizon_bars <= 256
            or calendar_valid_through <= calendar_valid_from
        ):
            raise ValueError("Kronos research horizon is invalid")
        self._adapter = adapter
        self._configuration = configuration
        self._calendar = calendar
        self._sampling = sampling
        self._horizon_bars = horizon_bars
        self._clock = clock
        self._calendar_valid_from = calendar_valid_from
        self._calendar_valid_through = calendar_valid_through

    def forecast(
        self,
        lease: JobLease,
        value: ResearchLeaseInput,
    ) -> Result[ForecastOutputArtifact]:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return _failure(
                ErrorCode.CONFIGURATION_INVALID,
                "Kronos forecast clock is invalid",
            )
        if now >= lease.deadline_at:
            return _failure(
                ErrorCode.DEADLINE_EXCEEDED,
                "Kronos forecast deadline exceeded",
            )
        forecast_as_of = min(value.request.as_of, now)
        local_date = forecast_as_of.astimezone(ZoneInfo(self._calendar.timezone)).date()
        if not self._calendar_valid_from <= local_date < self._calendar_valid_through:
            return _failure(
                ErrorCode.CONFIGURATION_INVALID,
                "Kronos exchange calendar is outside its verified window",
            )
        series = build_snapshot_bar_series(
            value,
            forecast_as_of=forecast_as_of,
        )
        if isinstance(series, Failure):
            return series
        request = self._request(lease, value, series.value, now=now)
        if isinstance(request, Failure):
            return request
        worker_request = build_kronos_worker_request(
            request.value,
            job_id=lease.job_id,
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            mic=self._calendar.mic,
            series=series.value,
            calendar=self._calendar,
            runtime=self._configuration.runtime,
            sampling=self._sampling,
            volume_quality=VolumeQuality.OBSERVED,
        )
        if isinstance(worker_request, Failure):
            return worker_request
        return self._adapter.forecast(worker_request.value, request.value)

    def _request(
        self,
        lease: JobLease,
        value: ResearchLeaseInput,
        series: BarSeries,
        *,
        now: datetime,
    ) -> Result[ForecastRequest]:
        runtime = self._configuration.runtime
        try:
            return Success(
                ForecastRequest(
                    request_id=uuid5(lease.run_id, "kronos-forecast-request"),
                    run_id=lease.run_id,
                    instrument_id=series.instrument_id,
                    dataset_snapshot_id=value.snapshot.snapshot_id,
                    snapshot_artifact_ref=value.snapshot.artifact_ref,
                    data_hash=value.snapshot.content_hash,
                    as_of=series.as_of,
                    interval="1d",
                    horizon_bars=self._horizon_bars,
                    input_window_start=series.bars[0].event_time,
                    input_window_end=series.bars[-1].event_time,
                    model_id=runtime.model_id,
                    model_revision=runtime.model_revision,
                    model_artifact_hash=runtime.model_artifact_hash,
                    tokenizer_id=runtime.tokenizer_id,
                    tokenizer_revision=runtime.tokenizer_revision,
                    tokenizer_artifact_hash=runtime.tokenizer_artifact_hash,
                    runtime_hash=runtime.runtime_hash,
                    requested_at=now,
                    deadline_at=lease.deadline_at,
                )
            )
        except (ValidationError, ValueError):
            return _failure(
                ErrorCode.CONFLICT,
                "Kronos forecast request binding is invalid",
            )


def build_snapshot_bar_series(
    value: ResearchLeaseInput,
    *,
    forecast_as_of: datetime,
) -> Result[BarSeries]:
    """Convert only exact PIT daily evidence; no stale or intraday fallback."""

    if forecast_as_of.tzinfo is None or forecast_as_of.utcoffset() is None:
        return _failure(ErrorCode.INVALID_INPUT, "Forecast cutoff is invalid")
    candidates = tuple(
        item for item in value.evidence if item.kind is EvidenceKind.MARKET_DATA
    )
    if len(candidates) < 2:
        return _failure(
            ErrorCode.DATA_UNAVAILABLE,
            "Kronos requires at least two daily bars",
        )
    try:
        bars = tuple(_bar(item, forecast_as_of) for item in candidates)
    except _IntradayEvidence:
        return _failure(
            ErrorCode.INVALID_INPUT,
            "Kronos requires an exact daily snapshot",
        )
    except _FutureEvidence:
        return _failure(
            ErrorCode.CONFLICT,
            "Kronos snapshot contains future evidence",
        )
    except (InvalidOperation, KeyError, TypeError, ValidationError, ValueError):
        return _failure(
            ErrorCode.MODEL_OUTPUT_INVALID,
            "Kronos snapshot bar is invalid",
        )
    ordered = tuple(sorted(bars, key=lambda item: item.event_time))
    event_times = tuple(item.event_time for item in ordered)
    if len(event_times) != len(set(event_times)):
        return _failure(
            ErrorCode.CONFLICT,
            "Kronos snapshot contains duplicate bars",
        )
    selected = ordered[-_MAX_CONTEXT_BARS:]
    try:
        quality = _series_quality(candidates)
        return Success(
            BarSeries(
                series_id=uuid5(
                    value.snapshot.snapshot_id,
                    f"kronos-bars:{value.request.instrument_id}",
                ),
                instrument_id=uuid5(NAMESPACE_URL, value.request.instrument_id),
                interval="1d",
                adjustment="provider_adjusted",
                session="regular",
                as_of=forecast_as_of,
                provider=value.snapshot.provider,
                endpoint=value.snapshot.endpoint,
                raw_artifact_ref=value.snapshot.artifact_ref,
                source_payload_hash=value.snapshot.content_hash,
                quality=quality,
                bars=selected,
            )
        )
    except (ValidationError, ValueError):
        return _failure(
            ErrorCode.CONFLICT,
            "Kronos bar series binding is invalid",
        )


def _bar(item: EvidenceItem, forecast_as_of: datetime) -> Bar:
    payload = item.payload
    if payload.get("interval") != "1d":
        raise _IntradayEvidence
    if item.available_at > forecast_as_of or item.event_time > forecast_as_of:
        raise _FutureEvidence
    event_time = datetime.fromisoformat(str(payload["event_time"]))
    if event_time != item.event_time:
        raise ValueError("bar event time changed")
    return Bar(
        event_time=event_time,
        published_at=item.published_at or item.available_at,
        available_at=item.available_at,
        observed_at=item.observed_at,
        open=Decimal(str(payload["open"])),
        high=Decimal(str(payload["high"])),
        low=Decimal(str(payload["low"])),
        close=Decimal(str(payload["close"])),
        volume=Decimal(str(payload["volume"])),
    )


def _series_quality(evidence: tuple[EvidenceItem, ...]) -> DataQuality:
    statuses = {item.quality.status for item in evidence}
    if len(statuses) != 1:
        raise ValueError("snapshot bar quality statuses conflict")
    first = evidence[0].quality
    warnings = tuple(
        sorted({warning for item in evidence for warning in item.quality.warnings})
    )
    return DataQuality(
        status=first.status,
        completeness=min(item.quality.completeness for item in evidence),
        warnings=warnings,
    )


class _IntradayEvidence(Exception):
    pass


class _FutureEvidence(Exception):
    pass


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
