"""Typed read model and immutable NAV persistence boundaries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.ledger import LedgerProjection
from stonks_agent.domain.monitoring import PortfolioValuation
from stonks_agent.domain.projections import PortfolioProjection, RiskProjection


@runtime_checkable
class PaperProjectionPort(Protocol):
    def save_valuation(
        self, valuation: PortfolioValuation
    ) -> Result[PortfolioValuation]: ...

    def get_portfolio(self, account_id: str) -> Result[PortfolioProjection]: ...

    def get_nav(self, account_id: str) -> Result[PortfolioValuation]: ...

    def get_risk(
        self, account_id: str, *, as_of: datetime
    ) -> Result[RiskProjection]: ...


@runtime_checkable
class LedgerProjectionReader(Protocol):
    def get_projection(self, account_id: str) -> Result[LedgerProjection]: ...


@runtime_checkable
class PaperProjectionUnitOfWork(Protocol):
    projections: PaperProjectionPort
    ledger: LedgerProjectionReader

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


type PaperProjectionUnitOfWorkFactory = Callable[[], PaperProjectionUnitOfWork]
