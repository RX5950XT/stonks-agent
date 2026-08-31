"""Typed read-only port for instrument company data."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.instrument_data import InstrumentDataQuery, InstrumentOverview


@runtime_checkable
class InstrumentDataSource(Protocol):
    def fetch(
        self,
        query: InstrumentDataQuery,
        *,
        observed_at: datetime,
    ) -> Result[InstrumentOverview]: ...
