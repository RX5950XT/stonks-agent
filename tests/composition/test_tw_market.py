from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from stonks_agent.application.market_freshness import MarketRegionFreshnessPolicy
from stonks_agent.composition.market_calendars import (
    VERIFIED_MARKETS,
    verified_market_freshness_policy,
)
from stonks_agent.composition.tw_market import (
    XTAI_2026_VALID_FROM,
    XTAI_2026_VALID_THROUGH,
    xtai_2026_calendar,
)
from stonks_agent.domain.latest_market_data import BarInterval, MarketDataFreshness

TAIPEI = ZoneInfo("Asia/Taipei")


def test_regular_session_is_0900_to_1330_taipei() -> None:
    session = xtai_2026_calendar().session_for(date(2026, 7, 30))

    assert session is not None
    assert session.mic == "XTAI"
    assert session.opens_at == datetime(2026, 7, 30, 9, tzinfo=TAIPEI)
    assert session.closes_at == datetime(2026, 7, 30, 13, 30, tzinfo=TAIPEI)


@pytest.mark.parametrize(
    "closed",
    [
        date(2026, 1, 1),  # 開國紀念日
        date(2026, 2, 12),  # settlement only, no trading
        date(2026, 2, 18),  # 春節
        date(2026, 2, 27),  # 和平紀念日 observed
        date(2026, 4, 6),  # 掃墓節 observed
        date(2026, 9, 28),  # Teachers Day
        date(2026, 10, 26),  # 光復節 observed
        date(2026, 12, 25),  # 行憲紀念日
    ],
)
def test_official_twse_closures_have_no_session(closed: date) -> None:
    assert xtai_2026_calendar().session_for(closed) is None


@pytest.mark.parametrize(
    "trading",
    [
        date(2026, 1, 2),  # 國曆新年開始交易日
        date(2026, 2, 11),  # 農曆春節前最後交易日
        date(2026, 2, 23),  # 農曆春節後開始交易日
    ],
)
def test_twse_entries_naming_trading_days_are_not_treated_as_closures(
    trading: date,
) -> None:
    assert xtai_2026_calendar().session_for(trading) is not None


def test_weekends_have_no_session() -> None:
    assert xtai_2026_calendar().session_for(date(2026, 7, 25)) is None


def test_calendar_window_covers_exactly_the_published_year() -> None:
    assert date(2026, 1, 1) == XTAI_2026_VALID_FROM
    assert date(2027, 1, 1) == XTAI_2026_VALID_THROUGH


def test_open_session_daily_bars_are_delayed_not_current() -> None:
    policy = verified_market_freshness_policy()

    assessed = policy.assess(
        market="TW",
        interval=BarInterval.DAY,
        latest_event_time=datetime(2026, 7, 30, tzinfo=UTC),
        checked_at=datetime(2026, 7, 30, 10, tzinfo=TAIPEI),
    )

    assert assessed is MarketDataFreshness.DELAYED


def test_taipei_and_new_york_are_assessed_against_their_own_calendars() -> None:
    policy = verified_market_freshness_policy()
    # 2026-02-18 is a TWSE 春節 closure and an ordinary XNAS trading day.
    checked = datetime(2026, 2, 18, 10, tzinfo=TAIPEI)

    taiwan = policy.assess(
        market="TW",
        interval=BarInterval.MINUTE,
        latest_event_time=checked,
        checked_at=checked,
    )

    assert taiwan is not MarketDataFreshness.CURRENT


def test_unverified_market_assesses_as_unknown_instead_of_borrowing_a_calendar() -> (
    None
):
    policy = verified_market_freshness_policy()

    assert "HK" not in VERIFIED_MARKETS
    assessed = policy.assess(
        market="HK",
        interval=BarInterval.DAY,
        latest_event_time=datetime(2026, 7, 30, tzinfo=UTC),
        checked_at=datetime(2026, 7, 30, 10, tzinfo=UTC),
    )

    assert assessed is MarketDataFreshness.UNKNOWN


def test_verified_markets_match_the_composed_calendars() -> None:
    policy = verified_market_freshness_policy()

    assert isinstance(policy, MarketRegionFreshnessPolicy)
    assert set(policy.calendars) == VERIFIED_MARKETS
