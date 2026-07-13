"""Typed persistence port for the canonical paper-trading aggregate."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from stonks_agent.domain.errors import Result
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.orders import OrderEvent, OrderIntent
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    PaperAccountState,
    PortfolioTarget,
)
from stonks_agent.domain.reservations import ReservationMutation
from stonks_agent.domain.risk import RiskDecision
from stonks_agent.domain.trading_persistence import ReservationOrderRecord


@runtime_checkable
class TradingRepositoryPort(Protocol):
    def register_account(
        self, snapshot: AccountPortfolioSnapshot, *, base_currency: str
    ) -> Result[PaperAccountState]: ...

    def get_account(self, account_id: str) -> Result[PaperAccountState]: ...

    def save_target(self, target: PortfolioTarget) -> Result[PortfolioTarget]: ...

    def save_risk_decision(self, decision: RiskDecision) -> Result[RiskDecision]: ...

    def create_reservation_order(
        self, mutation: ReservationMutation, intent: OrderIntent
    ) -> Result[ReservationOrderRecord]: ...

    def append_order_event(self, event: OrderEvent) -> Result[OrderEvent]: ...

    def list_order_events(self, intent_id: UUID) -> Result[tuple[OrderEvent, ...]]: ...

    def save_fill(self, fill: Fill) -> Result[Fill]: ...

    def append_journal(
        self,
        transaction: JournalTransaction,
        *,
        expected_account_sequence: int,
    ) -> Result[JournalTransaction]: ...

    def list_journal(
        self, account_id: str
    ) -> Result[tuple[JournalTransaction, ...]]: ...
