from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.strategies.baselines.common import (
    BaselineAlgorithm,
    BaselineSeries,
    load_baseline_manifests,
)
from stonks_agent.strategies.baselines.last_value import LastValueBaseline
from stonks_agent.strategies.baselines.linear import LinearBaseline
from stonks_agent.strategies.baselines.moving_average import MovingAverageBaseline
from stonks_contracts.market_data import Bar, DataQuality, DataQualityStatus

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 13, 2, tzinfo=UTC)
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000401")
SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000402")
HASH = "a" * 64


def bar(day: int, close: str, *, available_offset: int = 0) -> Bar:
    event_time = NOW - timedelta(days=5 - day)
    available_at = event_time + timedelta(days=available_offset)
    price = Decimal(close)
    return Bar(
        event_time=event_time,
        published_at=event_time,
        available_at=available_at,
        observed_at=max(event_time, available_at),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("100"),
    )


def series(*, bars: tuple[Bar, ...] | None = None) -> BaselineSeries:
    return BaselineSeries(
        instrument_id=INSTRUMENT_ID,
        dataset_snapshot_id=SNAPSHOT_ID,
        data_hash=HASH,
        as_of=NOW,
        interval="1d",
        horizon_bars=2,
        bars=bars
        or (
            bar(1, "100"),
            bar(2, "102"),
            bar(3, "101"),
            bar(4, "104"),
            bar(5, "105"),
        ),
        input_quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal("1"),
        ),
    )


def test_baseline_manifests_are_closed_deterministic_and_draft_only() -> None:
    manifests = load_baseline_manifests(ROOT / "config" / "strategies" / "baselines")

    assert set(manifests) == set(BaselineAlgorithm)
    assert all(value.deterministic is True for value in manifests.values())
    assert all(value.promotion_state == "draft" for value in manifests.values())
    assert all(value.kind == "deterministic" for value in manifests.values())


def test_three_baselines_match_golden_projection() -> None:
    manifests = load_baseline_manifests(ROOT / "config" / "strategies" / "baselines")
    strategies = {
        BaselineAlgorithm.LAST_VALUE: LastValueBaseline(
            manifests[BaselineAlgorithm.LAST_VALUE]
        ),
        BaselineAlgorithm.MOVING_AVERAGE: MovingAverageBaseline(
            manifests[BaselineAlgorithm.MOVING_AVERAGE]
        ),
        BaselineAlgorithm.LINEAR: LinearBaseline(manifests[BaselineAlgorithm.LINEAR]),
    }
    expected = json.loads(
        (ROOT / "tests" / "golden" / "baselines" / "forecasts.json").read_text(
            encoding="utf-8"
        )
    )

    actual = {
        algorithm.value: _projection(strategy.forecast(series()))
        for algorithm, strategy in strategies.items()
    }

    assert actual == expected


def test_baseline_replay_is_ordered_and_hash_stable() -> None:
    manifest = load_baseline_manifests(ROOT / "config" / "strategies" / "baselines")[
        BaselineAlgorithm.LINEAR
    ]
    strategy = LinearBaseline(manifest)

    first = strategy.forecast(series())
    second = strategy.forecast(series())

    assert first == second
    assert first.payload_hash() == second.payload_hash()


@pytest.mark.parametrize(
    "invalid_bars",
    [
        (bar(1, "100"), bar(1, "101")),
        (bar(1, "100"), bar(5, "101", available_offset=1)),
        (bar(1, "100"), bar(2, "0")),
    ],
)
def test_baseline_series_rejects_duplicate_future_or_nonpositive_prices(
    invalid_bars: tuple[Bar, ...],
) -> None:
    with pytest.raises(ValidationError):
        series(bars=invalid_bars)


def test_lookback_is_enforced_and_forecast_has_no_execution_authority() -> None:
    manifest = load_baseline_manifests(ROOT / "config" / "strategies" / "baselines")[
        BaselineAlgorithm.MOVING_AVERAGE
    ]
    strategy = MovingAverageBaseline(manifest)

    with pytest.raises(ValueError, match="lookback"):
        strategy.forecast(series(bars=(bar(1, "100"), bar(2, "101"))))
    signal = strategy.forecast(series())
    for forbidden in ("target_weight", "quantity", "order_intent", "risk_override"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(signal).model_validate(signal.model_dump() | {forbidden: "1"})


def _projection(signal: object) -> dict[str, object]:
    payload = signal.model_dump(mode="json")  # type: ignore[attr-defined]
    return {
        "model_id": payload["model_id"],
        "model_revision": payload["model_revision"],
        "expected_return": payload["expected_return"],
        "median_return": payload["median_return"],
        "direction_probability": payload["direction_probability"],
        "expected_volatility": payload["expected_volatility"],
        "downside_quantile": payload["downside_quantile"],
        "max_drawdown_quantile": payload["max_drawdown_quantile"],
        "dispersion": payload["dispersion"],
        "path_count": payload["path_count"],
    }
