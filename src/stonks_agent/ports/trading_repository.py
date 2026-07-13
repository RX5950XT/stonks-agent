"""Typed persistence port for the canonical paper-trading aggregate."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from stonks_agent.domain.errors import Result
from stonks_agent.domain.execution_model import PaperExecutionOutcome
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.orders import ExecutionCommand, OrderEvent, OrderIntent
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    PaperAccountState,
    PortfolioTarget,
)
from stonks_agent.domain.reservations import AccountReservation, ReservationMutation
from stonks_agent.domain.risk import RiskDecision
from stonks_agent.domain.trading_persistence import (
    PaperExecutionRecord,
    ReservationOrderBatchRecord,
    ReservationOrderRecord,
)


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

    def create_reservation_orders(
        self,
        pairs: tuple[tuple[ReservationMutation, OrderIntent], ...],
    ) -> Result[ReservationOrderBatchRecord]: ...

    def append_order_event(self, event: OrderEvent) -> Result[OrderEvent]: ...

    def list_order_events(self, intent_id: UUID) -> Result[tuple[OrderEvent, ...]]: ...

    def get_order_by_idempotency(
        self, *, account_id: str, idempotency_key: str
    ) -> Result[OrderIntent]: ...

    def get_reservation(self, reservation_id: UUID) -> Result[AccountReservation]: ...

    def list_fills(self, intent_id: UUID) -> Result[tuple[Fill, ...]]: ...

    def get_execution_record(
        self, *, account_id: str, idempotency_key: str
    ) -> Result[PaperExecutionRecord]: ...

    def apply_paper_execution(
        self,
        command: ExecutionCommand,
        outcome: PaperExecutionOutcome,
        *,
        expected_account_sequence: int,
    ) -> Result[PaperExecutionRecord]: ...

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
