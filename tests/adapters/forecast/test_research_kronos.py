from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.adapters.forecast.research_kronos import (
    build_snapshot_bar_series,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.research_job import (
    ResearchLeaseInput,
    SnapshotForecastContext,
)
from stonks_agent.domain.research_run import ResearchRunRequest
from stonks_contracts.evidence import EvidenceItem, EvidenceKind
from stonks_contracts.market_data import DataQuality, DataQualityStatus

NOW = datetime(2026, 7, 29, 18, tzinfo=UTC)
SNAPSHOT_ID = UUID("92300000-0000-4000-8000-000000000001")


def _context() -> SnapshotForecastContext:
    return SnapshotForecastContext(
        snapshot_id=SNAPSHOT_ID,
        manifest_artifact_hash="a" * 64,
        content_hash="a" * 64,
        provider="openbb_rest",
        endpoint="/api/v1/equity/price/historical",
    )


def _request() -> ResearchRunRequest:
    return ResearchRunRequest(
        instrument_id="instrument:aapl",
        symbol="AAPL",
        as_of=NOW,
        snapshot_id=SNAPSHOT_ID,
        research_profile_id="balanced/1",
        model_policy_id="research-models-v1",
        language="zh-TW",
        idempotency_key="research-kronos-test",
        owner_subject="local-console-research",
        requested_at=NOW,
    )


def _bar(index: int, *, interval: str = "1d") -> EvidenceItem:
    event_time = NOW - timedelta(days=3 - index)
    close = Decimal("100") + index
    return EvidenceItem(
        evidence_id=UUID(f"92300000-0000-4000-8000-{index + 2:012d}"),
        subject="instrument:aapl",
        kind=EvidenceKind.MARKET_DATA,
        payload={
            "event_time": event_time.isoformat(),
            "interval": interval,
            "open": str(close - 1),
            "high": str(close + 1),
            "low": str(close - 2),
            "close": str(close),
            "volume": str(1_000 + index),
        },
        event_time=event_time,
        published_at=None,
        available_at=NOW - timedelta(minutes=3 - index),
        observed_at=NOW,
        as_of=NOW,
        source="openbb-rest",
        provider="openbb_rest",
        content_hash=f"{index + 1:x}" * 64,
        raw_artifact_ref=f"sha256:{'b' * 64}",
        quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal("1"),
        ),
        sensitivity="internal",
        license_tag="provider-terms",
        redistribution_tag="internal-use-only",
        untrusted_content=True,
    )


def _lease_input(*items: EvidenceItem) -> ResearchLeaseInput:
    return ResearchLeaseInput(
        request=_request(),
        snapshot=_context(),
        evidence=items,
    )


def test_daily_snapshot_evidence_builds_sorted_snapshot_bound_bar_series() -> None:
    result = build_snapshot_bar_series(
        _lease_input(_bar(2), _bar(0), _bar(1)),
        forecast_as_of=NOW,
    )

    assert isinstance(result, Success)
    assert tuple(bar.close for bar in result.value.bars) == (
        Decimal("100"),
        Decimal("101"),
        Decimal("102"),
    )
    assert result.value.interval == "1d"
    assert result.value.provider == "openbb_rest"
    assert result.value.raw_artifact_ref == f"sha256:{'a' * 64}"
    assert result.value.bars[0].published_at == result.value.bars[0].available_at


def test_intraday_or_future_snapshot_evidence_fails_closed() -> None:
    intraday = build_snapshot_bar_series(
        _lease_input(_bar(0), _bar(1, interval="1h")),
        forecast_as_of=NOW,
    )
    future = build_snapshot_bar_series(
        _lease_input(_bar(0), _bar(1)),
        forecast_as_of=NOW - timedelta(minutes=10),
    )

    assert isinstance(intraday, Failure)
    assert intraday.error.code is ErrorCode.INVALID_INPUT
    assert isinstance(future, Failure)
    assert future.error.code is ErrorCode.CONFLICT


def test_duplicate_or_too_short_bar_window_fails_closed() -> None:
    original = _bar(0)
    duplicate = _bar(1).model_copy(
        update={
            "event_time": original.event_time,
            "payload": _bar(1).payload
            | {"event_time": original.event_time.isoformat()},
        }
    )

    repeated = build_snapshot_bar_series(
        _lease_input(original, duplicate),
        forecast_as_of=NOW,
    )
    short = build_snapshot_bar_series(
        _lease_input(_bar(0)),
        forecast_as_of=NOW,
    )

    assert isinstance(repeated, Failure)
    assert repeated.error.code is ErrorCode.CONFLICT
    assert isinstance(short, Failure)
    assert short.error.code is ErrorCode.DATA_UNAVAILABLE
