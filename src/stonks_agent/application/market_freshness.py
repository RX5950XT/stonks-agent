"""Exchange-session-aware freshness without pretending historical bars are ticks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from stonks_agent.domain.calendar import ExchangeCalendar, MarketSession
from stonks_agent.domain.latest_market_data import (
    BarInterval,
    MarketDataFreshness,
)

_CURRENT_AGE_SECONDS = {
    BarInterval.MINUTE: 180,
    BarInterval.FIVE_MINUTE: 480,
    BarInterval.FIFTEEN_MINUTE: 1_080,
    BarInterval.HOUR: 3_900,
}
_DELAYED_AGE_SECONDS = {
    BarInterval.MINUTE: 900,
    BarInterval.FIVE_MINUTE: 1_800,
    BarInterval.FIFTEEN_MINUTE: 3_600,
    BarInterval.HOUR: 7_200,
}


class MarketFreshnessPolicy(Protocol):
    def assess(
        self,
        *,
        interval: BarInterval,
        latest_event_time: datetime,
        checked_at: datetime,
    ) -> MarketDataFreshness: ...


@dataclass(frozen=True, slots=True)
class ExchangeMarketFreshnessPolicy:
    """Assess freshness only inside one explicitly verified calendar window."""

    calendar: ExchangeCalendar
    valid_from: date
    valid_through: date

    def __post_init__(self) -> None:
        if self.valid_through <= self.valid_from:
            raise ValueError("freshness calendar window is invalid")

    def assess(
        self,
        *,
        interval: BarInterval,
        latest_event_time: datetime,
        checked_at: datetime,
    ) -> MarketDataFreshness:
        event = _utc(latest_event_time)
        checked = _utc(checked_at)
        if event is None or checked is None or event > checked:
            return MarketDataFreshness.UNKNOWN
        local_date = checked.astimezone(ZoneInfo(self.calendar.timezone)).date()
        if not self.valid_from <= local_date < self.valid_through:
            return MarketDataFreshness.UNKNOWN
        session = self.calendar.session_for(local_date)
        if session is not None and session.is_open_at(checked):
            return _open_session_freshness(interval, event, checked)
        completed = _latest_completed_session(self.calendar, checked, local_date)
        if completed is None:
            return MarketDataFreshness.UNKNOWN
        return _closed_session_freshness(interval, event, completed)


def _open_session_freshness(
    interval: BarInterval,
    event: datetime,
    checked: datetime,
) -> MarketDataFreshness:
    if interval is BarInterval.DAY:
        return MarketDataFreshness.DELAYED
    age = int((checked - event).total_seconds())
    if age <= _CURRENT_AGE_SECONDS[interval]:
        return MarketDataFreshness.CURRENT
    if age <= _DELAYED_AGE_SECONDS[interval]:
        return MarketDataFreshness.DELAYED
    return MarketDataFreshness.STALE


def _closed_session_freshness(
    interval: BarInterval,
    event: datetime,
    completed: MarketSession,
) -> MarketDataFreshness:
    if interval is BarInterval.DAY:
        return (
            MarketDataFreshness.MARKET_CLOSED
            if event.date() == completed.session_date
            else MarketDataFreshness.STALE
        )
    if completed.opens_at <= event <= completed.closes_at:
        return MarketDataFreshness.MARKET_CLOSED
    return MarketDataFreshness.STALE


def _latest_completed_session(
    calendar: ExchangeCalendar,
    checked: datetime,
    local_date: date,
) -> MarketSession | None:
    for offset in range(10):
        session = calendar.session_for(local_date - timedelta(days=offset))
        if session is not None and session.closes_at <= checked:
            return session
    return None


def _utc(value: datetime) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)
