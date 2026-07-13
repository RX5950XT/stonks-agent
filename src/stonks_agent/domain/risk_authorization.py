"""Frozen inputs and result for atomic paper-order authorization."""

from __future__ import annotations

from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel
from stonks_agent.domain.orders import OrderType, TimeInForce
from stonks_agent.domain.risk import RiskDecision
from stonks_agent.domain.risk_evaluation import BuildRiskDecisionCommand
from stonks_agent.domain.trading_persistence import ReservationOrderBatchRecord
from stonks_contracts.common import NonEmptyString, PositiveDecimal


class PlannedPaperOrder(TradingModel):
    instrument_id: UUID
    reservation_id: UUID
    intent_id: UUID
    run_id: UUID
    idempotency_key: NonEmptyString
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: PositiveDecimal | None = None
    stop_price: PositiveDecimal | None = None
    execution_model_version: NonEmptyString


class RiskAuthorizationCommand(TradingModel):
    risk: BuildRiskDecisionCommand
    orders: tuple[PlannedPaperOrder, ...] = Field(max_length=100_000)

    @model_validator(mode="after")
    def validate_order_identity(self) -> Self:
        instruments = tuple(item.instrument_id for item in self.orders)
        if instruments != tuple(sorted(instruments, key=str)):
            raise ValueError("planned paper orders must be stably sorted")
        identities = tuple(
            (item.reservation_id, item.intent_id, item.idempotency_key)
            for item in self.orders
        )
        if len(instruments) != len(set(instruments)) or len(identities) != len(
            set(identities)
        ):
            raise ValueError("planned paper order identities must be unique")
        return self


class RiskAuthorizationResult(TradingModel):
    decision: RiskDecision
    orders: ReservationOrderBatchRecord | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        deltas = (
            tuple(
                item.delta_quantity
                for item in self.decision.normalized_target.allocations
                if item.delta_quantity != Decimal(0)
            )
            if self.decision.normalized_target is not None
            else ()
        )
        if self.orders is not None and not self.decision.approved:
            raise ValueError("rejected risk decision cannot authorize orders")
        if self.decision.approved and bool(deltas) != (self.orders is not None):
            raise ValueError("approved risk decision order batch is incomplete")
        return self
