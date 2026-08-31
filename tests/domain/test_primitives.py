from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.time import normalize_utc


def test_timezone_aware_values_are_normalized_to_utc() -> None:
    local = datetime(2026, 7, 10, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    result = normalize_utc(local)

    assert isinstance(result, Success)
    assert result.value == datetime(2026, 7, 10, 1, 0, tzinfo=UTC)


def test_naive_datetime_fails_closed() -> None:
    result = normalize_utc(datetime(2026, 7, 10, 9, 0))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
