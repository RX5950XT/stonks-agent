"""Deterministic portfolio snapshot and target contracts."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import model_validator

from .common import (
    ContractModel,
    Currency,
    DecimalString,
    Money,
    NonNegativeDecimal,
    Sha256,
    SignedUnitDecimal,
    UTCDateTime,
)


class CashBalance(ContractModel):
    currency: Currency
    amount: DecimalString
    available_amount: DecimalString


class Position(ContractModel):
    instrument_id: UUID
    quantity: DecimalString
    sellable_quantity: DecimalString
    average_cost: Money | None = None


class MarketPrice(ContractModel):
    instrument_id: UUID
    currency: Currency
    price: NonNegativeDecimal
    as_of: UTCDateTime


class PortfolioSnapshot(ContractModel):
    snapshot_id: UUID
    account_id: str
    as_of: UTCDateTime
    cash: tuple[CashBalance, ...]
    nav: tuple[Money, ...]
    positions: tuple[Position, ...] = ()
    pending_order_ids: tuple[UUID, ...] = ()
    prices: tuple[MarketPrice, ...] = ()
    fx_rates_hash: Sha256 | None = None
    ledger_sequence: int
    ledger_hash: Sha256


class TargetAllocation(ContractModel):
    instrument_id: UUID
    target_weight: SignedUnitDecimal | None = None
    current_quantity: DecimalString
    target_quantity: DecimalString | None = None
    delta_quantity: DecimalString | None = None
    constraint_diagnostics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.target_weight is None and self.target_quantity is None:
            raise ValueError("target_weight or target_quantity is required")
        if self.target_quantity is not None and self.delta_quantity is not None:
            expected = self.target_quantity - self.current_quantity
            if self.delta_quantity != expected:
                raise ValueError("delta_quantity must equal target_quantity - current_quantity")
        return self


class PortfolioTarget(ContractModel):
    target_id: UUID
    account_id: str
    portfolio_snapshot_id: UUID
    as_of: UTCDateTime
    allocations: tuple[TargetAllocation, ...]
    input_signal_ids: tuple[UUID, ...]
    input_evidence_ids: tuple[UUID, ...] = ()
    policy_version: str
    expected_turnover: NonNegativeDecimal
    expected_cost: Money
    calculation_hash: Sha256

    @property
    def target_weight(self) -> DecimalString | None:
        """Return the single-instrument weight used by the P0 vertical slice."""
        if len(self.allocations) != 1:
            return None
        return self.allocations[0].target_weight
