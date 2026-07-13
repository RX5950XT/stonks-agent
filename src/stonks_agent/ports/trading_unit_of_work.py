"""Minimal transaction owner used by paper risk authorization."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from stonks_agent.ports.ledger import LedgerPort
from stonks_agent.ports.trading_repository import TradingRepositoryPort


@runtime_checkable
class TradingUnitOfWork(Protocol):
    trading: TradingRepositoryPort
    ledger: LedgerPort

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


type TradingUnitOfWorkFactory = Callable[[], TradingUnitOfWork]


class TradingCommitError(RuntimeError):
    """Public-safe signal that the authoritative transaction did not commit."""
