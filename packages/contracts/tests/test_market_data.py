from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_contracts.market_data import Bar, BarSeries

NOW = datetime(2026, 7, 10, 8, 30, tzinfo=UTC)
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000001")


def _bar(offset: int, **overrides: str) -> Bar:
    values: dict[str, object] = {
        "event_time": NOW + timedelta(days=offset),
        "published_at": NOW + timedelta(days=offset, minutes=1),
        "available_at": NOW + timedelta(days=offset, minutes=2),
        "observed_at": NOW + timedelta(days=offset, minutes=3),
        "open": "100",
        "high": "102",
        "low": "99",
        "close": "101",
        "volume": "1000",
    }
    values.update(overrides)
    return Bar.model_validate(values)


def test_bar_rejects_invalid_ohlc() -> None:
    with pytest.raises(ValidationError, match="low <= open/close <= high"):
        _bar(0, high="100", close="101")


def test_bar_series_requires_strictly_increasing_unique_times() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        BarSeries(
            series_id=UUID("00000000-0000-4000-8000-000000000030"),
            instrument_id=INSTRUMENT_ID,
            interval="1d",
            adjustment="split_dividend",
            session="regular",
            as_of=NOW + timedelta(days=2),
            provider="fixture",
            endpoint="replay",
            raw_artifact_ref="sha256:" + "c" * 64,
            bars=(_bar(1), _bar(0)),
        )
