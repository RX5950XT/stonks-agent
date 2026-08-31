"""Bounded failover between validated latest-market data sources."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.latest_market_data import (
    LatestMarketDataObservation,
    LatestMarketDataQuery,
)
from stonks_agent.ports.latest_market_data import LatestMarketDataSource


class FailoverLatestMarketDataSource:
    """Try fixed sources in order and never replace a failure with fake data."""

    __slots__ = ("_sources",)

    def __init__(
        self,
        sources: Sequence[tuple[str, LatestMarketDataSource]],
    ) -> None:
        values = tuple(sources)
        labels = tuple(label for label, _ in values)
        if (
            not values
            or len(labels) != len(set(labels))
            or any(not label or label.strip() != label for label in labels)
        ):
            raise ValueError("latest market data sources are invalid")
        self._sources = values

    def fetch(
        self,
        query: LatestMarketDataQuery,
        *,
        observed_at: datetime,
    ) -> Result[LatestMarketDataObservation]:
        attempts: list[tuple[str, str]] = []
        for label, source in self._sources:
            try:
                result = source.fetch(query, observed_at=observed_at)
            except Exception:
                attempts.append((label, ErrorCode.DATA_UNAVAILABLE.value))
                continue
            if isinstance(result, Failure):
                attempts.append((label, result.error.code.value))
                continue
            if attempts:
                warnings = (*result.value.warnings, "fallback_source_used")[-16:]
                return Success(result.value.model_copy(update={"warnings": warnings}))
            return result
        return Failure(
            StructuredError(
                code=_failure_code(attempts),
                message="All market-data sources are unavailable",
                details={"attempted_sources": tuple(attempts)},
            )
        )


def _failure_code(attempts: Sequence[tuple[str, str]]) -> ErrorCode:
    codes = {code for _, code in attempts}
    if codes == {ErrorCode.RATE_LIMITED.value}:
        return ErrorCode.RATE_LIMITED
    if codes == {ErrorCode.CAPABILITY_DENIED.value}:
        return ErrorCode.CAPABILITY_DENIED
    if codes == {ErrorCode.NOT_FOUND.value}:
        return ErrorCode.NOT_FOUND
    return ErrorCode.DATA_UNAVAILABLE
