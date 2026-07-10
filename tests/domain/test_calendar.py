from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from stonks_agent.domain.calendar import ExchangeCalendar, SessionTemplate


def nyse_calendar() -> ExchangeCalendar:
    return ExchangeCalendar(
        mic="XNYS",
        timezone="America/New_York",
        default=SessionTemplate(open_time=time(9, 30), close_time=time(16)),
        holidays=frozenset({date(2026, 1, 1)}),
    )


def test_calendar_applies_dst_in_exchange_timezone() -> None:
    calendar = nyse_calendar()

    winter = calendar.session_for(date(2026, 3, 6))
    summer = calendar.session_for(date(2026, 3, 9))

    assert winter is not None and summer is not None
    assert winter.opens_at == datetime(2026, 3, 6, 14, 30, tzinfo=UTC)
    assert summer.opens_at == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)


def test_holiday_and_weekend_have_no_session() -> None:
    calendar = nyse_calendar()

    assert calendar.session_for(date(2026, 1, 1)) is None
    assert calendar.session_for(date(2026, 1, 3)) is None


def test_lunch_break_is_not_tradable() -> None:
    calendar = ExchangeCalendar(
        mic="XHKG",
        timezone="Asia/Hong_Kong",
        default=SessionTemplate(
            open_time=time(9, 30),
            close_time=time(16),
            break_start=time(12),
            break_end=time(13),
        ),
    )
    session = calendar.session_for(date(2026, 1, 2))

    assert session is not None
    assert session.is_open_at(datetime(2026, 1, 2, 3, 59, tzinfo=UTC))
    assert not session.is_open_at(datetime(2026, 1, 2, 4, 30, tzinfo=UTC))
    assert session.is_open_at(datetime(2026, 1, 2, 5, 0, tzinfo=UTC))


def test_overnight_session_closes_on_following_local_day() -> None:
    calendar = ExchangeCalendar(
        mic="XCBT",
        timezone="America/Chicago",
        default=SessionTemplate(open_time=time(18), close_time=time(17)),
    )
    session = calendar.session_for(date(2026, 1, 2))

    assert session is not None
    local_open = session.opens_at.astimezone(ZoneInfo("America/Chicago"))
    local_close = session.closes_at.astimezone(ZoneInfo("America/Chicago"))
    assert local_close.date() > local_open.date()
    assert session.closes_at > session.opens_at


def test_next_session_skips_weekend_and_holiday() -> None:
    calendar = nyse_calendar()

    session = calendar.next_session_after(datetime(2025, 12, 31, 22, tzinfo=UTC))

    assert session.session_date == date(2026, 1, 2)
