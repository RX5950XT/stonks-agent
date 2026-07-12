from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.market_data import OHLCBar

AS_OF = datetime(2026, 1, 2, 21, tzinfo=UTC)


def timeline(**overrides: object) -> EvidenceTimeline:
    payload: dict[str, object] = {
        "event_time": AS_OF - timedelta(minutes=2),
        "published_at": AS_OF - timedelta(minutes=1),
        "available_at": AS_OF,
        "observed_at": AS_OF,
        "as_of": AS_OF,
        "availability_certainty": AvailabilityCertainty.PROVEN,
        "strict_point_in_time": True,
    }
    payload.update(overrides)
    return EvidenceTimeline.model_validate(payload)


def test_future_evidence_is_rejected_in_strict_point_in_time_mode() -> None:
    with pytest.raises(ValidationError, match="future evidence"):
        timeline(available_at=AS_OF + timedelta(microseconds=1))


@given(
    offset_microseconds=st.integers(min_value=-86_400_000_000, max_value=86_400_000_000)
)
def test_strict_point_in_time_acceptance_is_exactly_bounded_by_as_of(
    offset_microseconds: int,
) -> None:
    available_at = AS_OF + timedelta(microseconds=offset_microseconds)
    payload = {
        "event_time": min(available_at, AS_OF) - timedelta(seconds=2),
        "published_at": min(available_at, AS_OF) - timedelta(seconds=1),
        "available_at": available_at,
        "observed_at": max(available_at, AS_OF),
        "as_of": AS_OF,
        "availability_certainty": AvailabilityCertainty.PROVEN,
        "strict_point_in_time": True,
    }

    if available_at > AS_OF:
        with pytest.raises(ValidationError, match="future evidence"):
            EvidenceTimeline.model_validate(payload)
    else:
        assert EvidenceTimeline.model_validate(payload).available_at == available_at


def test_unknown_availability_is_rejected_in_strict_mode() -> None:
    with pytest.raises(ValidationError, match="proven availability"):
        timeline(availability_certainty=AvailabilityCertainty.UNKNOWN)


def test_backfilled_observation_keeps_distinct_time_semantics() -> None:
    observed_later = AS_OF + timedelta(days=30)
    value = timeline(observed_at=observed_later)

    assert value.available_at == AS_OF
    assert value.observed_at == observed_later
    assert value.event_time < value.published_at < value.available_at


@given(
    low=st.decimals(min_value="0.01", max_value="999", places=2),
    spread=st.decimals(min_value="0.01", max_value="100", places=2),
    open_fraction=st.decimals(min_value="0", max_value="1", places=2),
    close_fraction=st.decimals(min_value="0", max_value="1", places=2),
)
def test_ohlc_invariant_accepts_values_inside_range(
    low: Decimal,
    spread: Decimal,
    open_fraction: Decimal,
    close_fraction: Decimal,
) -> None:
    high = low + spread

    bar = OHLCBar(
        timeline=timeline(),
        open=low + spread * open_fraction,
        high=high,
        low=low,
        close=low + spread * close_fraction,
        volume=Decimal("0"),
    )

    assert bar.low <= bar.open <= bar.high
    assert bar.low <= bar.close <= bar.high


@pytest.mark.parametrize(
    ("open_", "high", "low", "close"),
    [
        ("9", "8", "7", "8"),
        ("8", "9", "7", "10"),
        ("8", "7", "9", "8"),
    ],
)
def test_invalid_ohlc_fails_closed(
    open_: str,
    high: str,
    low: str,
    close: str,
) -> None:
    with pytest.raises(ValidationError, match="OHLC"):
        OHLCBar(
            timeline=timeline(),
            open=Decimal(open_),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(close),
            volume=Decimal("1"),
        )
