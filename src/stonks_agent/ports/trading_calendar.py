"""Trading-calendar boundary contract."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from stonks_agent.domain.calendar import MarketSession
from stonks_agent.domain.errors import Result


@runtime_checkable
class TradingCalendarPort(Protocol):
    def session_for(self, mic: str, session_date: date) -> Result[MarketSession]: ...

    def next_session_after(
        self, mic: str, value: datetime
    ) -> Result[MarketSession]: ...
