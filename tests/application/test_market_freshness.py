from __future__ import annotations

from datetime import UTC, date, datetime, time

from stonks_agent.application.market_freshness import (
    ExchangeMarketFreshnessPolicy,
)
from stonks_agent.domain.calendar import (
    ExchangeCalendar,
    SessionOverride,
    SessionTemplate,
)
from stonks_agent.domain.latest_market_data import (
    BarInterval,
    MarketDataFreshness,
)


def test_intraday_freshness_is_session_aware() -> None:
    policy = freshness_policy()

    current = policy.assess(
        interval=BarInterval.MINUTE,
        latest_event_time=datetime(2026, 7, 29, 18, 31, tzinfo=UTC),
        checked_at=datetime(2026, 7, 29, 18, 32, tzinfo=UTC),
    )
    delayed = policy.assess(
        interval=BarInterval.MINUTE,
        latest_event_time=datetime(2026, 7, 29, 18, 22, tzinfo=UTC),
        checked_at=datetime(2026, 7, 29, 18, 32, tzinfo=UTC),
    )
    stale = policy.assess(
        interval=BarInterval.MINUTE,
        latest_event_time=datetime(2026, 7, 29, 18, 10, tzinfo=UTC),
        checked_at=datetime(2026, 7, 29, 18, 32, tzinfo=UTC),
    )

    assert current is MarketDataFreshness.CURRENT
    assert delayed is MarketDataFreshness.DELAYED
    assert stale is MarketDataFreshness.STALE


def test_latest_completed_session_is_not_marked_stale_while_market_is_closed() -> None:
    policy = freshness_policy()

    after_close = policy.assess(
        interval=BarInterval.MINUTE,
        latest_event_time=datetime(2026, 7, 29, 19, 59, tzinfo=UTC),
        checked_at=datetime(2026, 7, 29, 22, tzinfo=UTC),
    )
    holiday = policy.assess(
        interval=BarInterval.MINUTE,
        latest_event_time=datetime(2026, 7, 2, 16, 59, tzinfo=UTC),
        checked_at=datetime(2026, 7, 3, 16, tzinfo=UTC),
    )

    assert after_close is MarketDataFreshness.MARKET_CLOSED
    assert holiday is MarketDataFreshness.MARKET_CLOSED


def test_daily_and_unverified_calendar_windows_fail_honestly() -> None:
    policy = freshness_policy()

    during_session = policy.assess(
        interval=BarInterval.DAY,
        latest_event_time=datetime(2026, 7, 28, tzinfo=UTC),
        checked_at=datetime(2026, 7, 29, 18, 32, tzinfo=UTC),
    )
    outside_verified_window = policy.assess(
        interval=BarInterval.MINUTE,
        latest_event_time=datetime(2027, 1, 4, 15, 31, tzinfo=UTC),
        checked_at=datetime(2027, 1, 4, 15, 32, tzinfo=UTC),
    )

    assert during_session is MarketDataFreshness.DELAYED
    assert outside_verified_window is MarketDataFreshness.UNKNOWN


def freshness_policy() -> ExchangeMarketFreshnessPolicy:
    regular = SessionTemplate(open_time=time(9, 30), close_time=time(16))
    early = SessionTemplate(open_time=time(9, 30), close_time=time(13))
    return ExchangeMarketFreshnessPolicy(
        calendar=ExchangeCalendar(
            mic="XNAS",
            timezone="America/New_York",
            default=regular,
            holidays=frozenset({date(2026, 7, 3)}),
            overrides=(SessionOverride(session_date=date(2026, 7, 2), template=early),),
        ),
        valid_from=date(2026, 1, 1),
        valid_through=date(2026, 12, 31),
    )
