from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.analytics.event_study import (
    AlignedReturn,
    EventStudyPolicy,
    EventStudyRequest,
    aggregate_window,
    analyze_event,
    bootstrap_mean_ci,
    fit_market_model,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success

GOLDEN = Path("tests/golden/event_study/known_market_model.json")
AS_OF = datetime(2026, 5, 12, 23, tzinfo=UTC)
EVENT_ID = UUID("31000000-0000-4000-8000-000000000001")
EVIDENCE_ID = UUID("31000000-0000-4000-8000-000000000002")
INSTRUMENT_ID = UUID("31000000-0000-4000-8000-000000000003")


def policy() -> EventStudyPolicy:
    return EventStudyPolicy(
        estimation_start=-5,
        estimation_end=-2,
        minimum_estimation_observations=4,
        event_window_end=2,
    )


def request(**overrides: object) -> EventStudyRequest:
    values: dict[str, object] = {
        "event_id": EVENT_ID,
        "evidence_id": EVIDENCE_ID,
        "instrument_id": INSTRUMENT_ID,
        "filing_available_at": datetime(2026, 5, 8, 22, tzinfo=UTC),
        "event_day": date(2026, 5, 10),
        "as_of": AS_OF,
    }
    values.update(overrides)
    return EventStudyRequest.model_validate(values)


def observations() -> tuple[AlignedReturn, ...]:
    market = ("0.01", "-0.02", "0.03", "0.00", "0.01", "0.02", "-0.01", "0.005")
    abnormal = ("0", "0", "0", "0", "0", "0.02", "0.01", "-0.005")
    result = []
    for index, (market_return, extra) in enumerate(zip(market, abnormal, strict=True)):
        day = date(2026, 5, 5) + timedelta(days=index)
        stock = (
            Decimal("0.001") + Decimal("1.2") * Decimal(market_return) + Decimal(extra)
        )
        result.append(
            AlignedReturn(
                trading_day=day,
                stock_return=stock,
                market_return=Decimal(market_return),
                available_at=datetime.combine(day, datetime.min.time(), UTC)
                + timedelta(hours=22),
            )
        )
    return tuple(result)


def test_known_market_model_car_matches_golden() -> None:
    result = analyze_event(request(), observations(), policy=policy())
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert isinstance(result, Success)
    assert result.value.model_dump(mode="json") == expected
    assert result.value.market_model.alpha == Decimal("0.001")
    assert result.value.market_model.beta == Decimal("1.2")
    assert result.value.car_0_2 == Decimal("0.025")


def test_future_unknown_duplicate_and_missing_event_data_fail_closed() -> None:
    future = observations()[0].model_copy(
        update={"available_at": AS_OF + timedelta(seconds=1)}
    )
    duplicate = (*observations(), observations()[0])
    missing_day = request(
        event_day=date(2026, 5, 13), as_of=datetime(2026, 5, 14, tzinfo=UTC)
    )
    after_close = request(
        filing_available_at=datetime(2026, 5, 10, 23, tzinfo=UTC),
        event_day=date(2026, 5, 10),
    )

    future_result = analyze_event(
        request(), (future, *observations()[1:]), policy=policy()
    )
    duplicate_result = analyze_event(request(), duplicate, policy=policy())
    missing_result = analyze_event(missing_day, observations(), policy=policy())
    after_close_result = analyze_event(after_close, observations(), policy=policy())

    for result in (
        future_result,
        duplicate_result,
        missing_result,
        after_close_result,
    ):
        assert isinstance(result, Failure)
        assert result.error.code in {ErrorCode.CONFLICT, ErrorCode.INVALID_INPUT}


def test_insufficient_estimation_window_fails_without_partial_car() -> None:
    result = analyze_event(request(), observations()[3:], policy=policy())

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE


def test_pure_market_model_and_zero_variance_are_finite() -> None:
    exact = fit_market_model(
        tuple(
            Decimal("0.001") + Decimal("1.2") * value
            for value in (Decimal(".01"), Decimal("-.02"), Decimal(".03"))
        ),
        (Decimal(".01"), Decimal("-.02"), Decimal(".03")),
    )
    flat = fit_market_model(
        (Decimal(".01"), Decimal(".02"), Decimal(".03")),
        (Decimal("0"), Decimal("0"), Decimal("0")),
    )

    assert exact.alpha == Decimal("0.001")
    assert exact.beta == Decimal("1.2")
    assert exact.r_squared == Decimal("1")
    assert flat.beta == Decimal("0")
    assert flat.alpha == Decimal("0.02")


def test_bootstrap_and_aggregate_statistics_are_seeded_and_bounded() -> None:
    values = (Decimal(".01"), Decimal(".02"), Decimal(".03"), Decimal(".04"))
    first = bootstrap_mean_ci(
        values, n_bootstrap=1000, confidence=Decimal(".95"), seed=42
    )
    second = bootstrap_mean_ci(
        values, n_bootstrap=1000, confidence=Decimal(".95"), seed=42
    )
    stats = aggregate_window("[0,+1]", values, n_bootstrap=1000, seed=42)

    assert first == second
    assert first.lower <= Decimal(".025") <= first.upper
    assert stats.n_events == 4
    assert stats.mean_car == Decimal(".025")
    assert Decimal("0") <= stats.p_value <= Decimal("1")
    assert stats.p_value < Decimal(".05")


def test_invalid_stats_inputs_fail_loudly() -> None:
    for stock, market in (
        ((), ()),
        ((Decimal("1"),), ()),
        ((Decimal("NaN"),), (Decimal("0"),)),
    ):
        try:
            fit_market_model(stock, market)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid market-model inputs must fail")


def test_timeline_policy_and_bootstrap_bounds_are_strict() -> None:
    with pytest.raises(ValidationError):
        AlignedReturn(
            trading_day=date(2026, 5, 5),
            stock_return=Decimal(".01"),
            market_return=Decimal(".01"),
            available_at=datetime(2026, 5, 4, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        request(filing_available_at=AS_OF + timedelta(seconds=1))
    with pytest.raises(ValidationError):
        EventStudyPolicy(estimation_start=-2, estimation_end=-5)
    with pytest.raises(ValueError, match="bootstrap count"):
        bootstrap_mean_ci(
            (Decimal(".01"), Decimal(".02")),
            n_bootstrap=99,
            confidence=Decimal(".95"),
            seed=1,
        )
    flat = aggregate_window(
        "[0,+1]",
        (Decimal(".01"), Decimal(".01")),
        n_bootstrap=100,
        seed=1,
    )
    assert flat.t_statistic == 0
    assert flat.p_value == 1
