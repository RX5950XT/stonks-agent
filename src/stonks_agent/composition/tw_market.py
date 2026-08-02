"""Versioned local Taiwan market calendar shared by freshness and Kronos.

Source of authority: the TWSE official holiday schedule OpenAPI
`https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule`, retrieved
2026-07-30, which returned exactly 27 entries covering ROC year 115 (2026).
Three of those entries name trading days rather than closures
(`國曆新年開始交易日`, `農曆春節前最後交易日`, `農曆春節後開始交易日`) and are
deliberately excluded; the settlement-only entries
(`market closed, settlement only`) are closures and are included. Six further
entries fall on weekends, which the default template already excludes. The
remaining 18 weekday closures are listed below. The schedule only covers 2026,
so the freshness window ends with it and later dates assess as unknown.
"""

from __future__ import annotations

from datetime import date, time

from stonks_agent.application.market_freshness import (
    ExchangeMarketFreshnessPolicy,
)
from stonks_agent.domain.calendar import ExchangeCalendar, SessionTemplate

XTAI_2026_VALID_FROM = date(2026, 1, 1)
XTAI_2026_VALID_THROUGH = date(2027, 1, 1)
_HOLIDAYS = frozenset(
    {
        date(2026, 1, 1),  # 中華民國開國紀念日
        date(2026, 2, 12),  # market closed, settlement only
        date(2026, 2, 13),  # market closed, settlement only
        date(2026, 2, 16),  # 農曆除夕及春節
        date(2026, 2, 17),  # 農曆除夕及春節
        date(2026, 2, 18),  # 農曆除夕及春節
        date(2026, 2, 19),  # 農曆除夕及春節
        date(2026, 2, 20),  # 農曆除夕及春節
        date(2026, 2, 27),  # 和平紀念日
        date(2026, 4, 3),  # 兒童節及民族掃墓節
        date(2026, 4, 6),  # 兒童節及民族掃墓節
        date(2026, 5, 1),  # 勞動節
        date(2026, 6, 19),  # 端午節
        date(2026, 9, 25),  # 中秋節
        date(2026, 9, 28),  # Teachers Day
        date(2026, 10, 9),  # 國慶日
        date(2026, 10, 26),  # 臺灣光復暨金門古寧頭大捷紀念日
        date(2026, 12, 25),  # 行憲紀念日
    }
)


def xtai_2026_calendar() -> ExchangeCalendar:
    """TWSE regular continuous trading, 09:00-13:30 Asia/Taipei."""

    return ExchangeCalendar(
        mic="XTAI",
        timezone="Asia/Taipei",
        default=SessionTemplate(open_time=time(9), close_time=time(13, 30)),
        holidays=_HOLIDAYS,
    )


def xtai_2026_freshness_policy() -> ExchangeMarketFreshnessPolicy:
    return ExchangeMarketFreshnessPolicy(
        calendar=xtai_2026_calendar(),
        valid_from=XTAI_2026_VALID_FROM,
        valid_through=XTAI_2026_VALID_THROUGH,
    )
