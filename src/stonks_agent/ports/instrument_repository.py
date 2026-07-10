"""Instrument reference-data repository contract."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from stonks_agent.domain.errors import Result
from stonks_agent.domain.instrument import Instrument


@runtime_checkable
class InstrumentRepository(Protocol):
    def get(self, instrument_id: UUID) -> Result[Instrument]: ...

    def resolve(
        self,
        provider: str,
        symbol: str,
        as_of: datetime,
    ) -> Result[Instrument]: ...

    def history(self, instrument_id: UUID) -> Result[tuple[Instrument, ...]]: ...
