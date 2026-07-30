"""Versioned local US market calendar shared by freshness and Kronos."""

from __future__ import annotations

from datetime import date, time

from stonks_agent.application.market_freshness import (
    ExchangeMarketFreshnessPolicy,
)
from stonks_agent.domain.calendar import (
    ExchangeCalendar,
    SessionOverride,
    SessionTemplate,
)

XNAS_2026_VALID_FROM = date(2026, 1, 1)
XNAS_2026_VALID_THROUGH = date(2027, 1, 1)
_HOLIDAYS = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }
)


def xnas_2026_calendar() -> ExchangeCalendar:
    regular = SessionTemplate(open_time=time(9, 30), close_time=time(16))
    early = SessionTemplate(open_time=time(9, 30), close_time=time(13))
    return ExchangeCalendar(
        mic="XNAS",
        timezone="America/New_York",
        default=regular,
        holidays=_HOLIDAYS,
        overrides=tuple(
            SessionOverride(session_date=value, template=early)
            for value in (
                date(2026, 7, 2),
                date(2026, 11, 27),
                date(2026, 12, 24),
            )
        ),
    )


def xnas_2026_freshness_policy() -> ExchangeMarketFreshnessPolicy:
    return ExchangeMarketFreshnessPolicy(
        calendar=xnas_2026_calendar(),
        valid_from=XNAS_2026_VALID_FROM,
        valid_through=XNAS_2026_VALID_THROUGH,
    )
