"""Canonical account snapshot and deterministic portfolio target invariants."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel, is_quantized
from stonks_contracts.common import (
    Currency,
    DecimalString,
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    SignedUnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64


class CashBalance(TradingModel):
    currency: Currency
    settled_amount: NonNegativeDecimal
    reserved_amount: NonNegativeDecimal
    quantum: PositiveDecimal

    @model_validator(mode="after")
    def validate_balance(self) -> Self:
        if self.reserved_amount > self.settled_amount:
            raise ValueError("reserved cash cannot exceed settled cash")
        if not all(
            is_quantized(value, self.quantum)
            for value in (self.settled_amount, self.reserved_amount)
        ):
            raise ValueError("cash amounts must match currency quantum")
        return self

    @property
    def available_amount(self) -> Decimal:
        return self.settled_amount - self.reserved_amount


class PositionBalance(TradingModel):
    instrument_id: UUID
    quantity: NonNegativeDecimal
    sellable_quantity: NonNegativeDecimal
    reserved_quantity: NonNegativeDecimal
    quantum: PositiveDecimal

    @model_validator(mode="after")
    def validate_balance(self) -> Self:
        if self.sellable_quantity > self.quantity:
            raise ValueError("sellable quantity cannot exceed position quantity")
        if self.reserved_quantity > self.sellable_quantity:
            raise ValueError("reserved quantity cannot exceed sellable quantity")
        if not all(
            is_quantized(value, self.quantum)
            for value in (
                self.quantity,
                self.sellable_quantity,
                self.reserved_quantity,
            )
        ):
            raise ValueError("position quantities must match instrument quantum")
        return self

    @property
    def available_quantity(self) -> Decimal:
        return self.sellable_quantity - self.reserved_quantity


class AccountPortfolioSnapshot(TradingModel):
    snapshot_id: UUID
    account_id: NonEmptyString
    as_of: UTCDateTime
    account_aggregate_sequence: int = Field(ge=0)
    portfolio_sequence: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    ledger_hash: Sha256 | None = None
    cash: tuple[CashBalance, ...] = Field(default_factory=tuple, max_length=64)
    positions: tuple[PositionBalance, ...] = Field(
        default_factory=tuple, max_length=100_000
    )
    pending_order_ids: tuple[UUID, ...] = Field(
        default_factory=tuple, max_length=100_000
    )

    @model_validator(mode="after")
    def validate_stable_identity_order(self) -> Self:
        if (self.ledger_sequence == 0) != (self.ledger_hash is None):
            raise ValueError("only genesis portfolio snapshot may omit ledger hash")
        _require_sorted_unique(
            tuple(item.currency for item in self.cash), "cash currencies"
        )
        _require_sorted_unique(
            tuple(str(item.instrument_id) for item in self.positions),
            "position instruments",
        )
        _require_sorted_unique(
            tuple(str(item) for item in self.pending_order_ids),
            "pending order ids",
        )
        return self

    @property
    def snapshot_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


class PaperAccountEvent(TradingModel):
    """Immutable account-aggregate audit event persisted with every CAS update."""

    event_id: UUID
    account_id: NonEmptyString
    sequence: int = Field(ge=1)
    event_type: NonEmptyString
    aggregate_ref_type: NonEmptyString
    aggregate_ref_id: UUID
    occurred_at: UTCDateTime
    previous_hash: Sha256 | None = None
    event_hash: Sha256

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if (self.sequence == 1) != (self.previous_hash is None):
            raise ValueError("only genesis account event may omit previous hash")
        if self.event_hash != self.expected_event_hash():
            raise ValueError("paper account event hash does not match payload")
        return self

    def expected_event_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json", exclude={"event_hash"}))


class PaperAccountState(TradingModel):
    """Database-authoritative account aggregate and its current projections."""

    account_id: NonEmptyString
    base_currency: Currency
    account_aggregate_sequence: int = Field(ge=0)
    portfolio_sequence: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    ledger_hash: Sha256 | None = None
    cash: tuple[CashBalance, ...] = Field(default_factory=tuple, max_length=64)
    positions: tuple[PositionBalance, ...] = Field(
        default_factory=tuple, max_length=100_000
    )
    events: tuple[PaperAccountEvent, ...] = Field(
        default_factory=tuple, max_length=1_000_000
    )
    created_at: UTCDateTime
    updated_at: UTCDateTime

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.ledger_sequence == 0) != (self.ledger_hash is None):
            raise ValueError("only genesis paper account may omit ledger hash")
        if self.updated_at < self.created_at:
            raise ValueError("paper account timeline is invalid")
        _require_sorted_unique(
            tuple(item.currency for item in self.cash), "cash currencies"
        )
        _require_sorted_unique(
            tuple(str(item.instrument_id) for item in self.positions),
            "position instruments",
        )
        self._validate_event_chain()
        return self

    def _validate_event_chain(self) -> None:
        if len(self.events) != self.account_aggregate_sequence:
            raise ValueError("paper account event count does not match sequence")
        previous_hash: str | None = None
        for sequence, event in enumerate(self.events, start=1):
            if (
                event.account_id != self.account_id
                or event.sequence != sequence
                or event.previous_hash != previous_hash
            ):
                raise ValueError("paper account event chain is invalid")
            previous_hash = event.event_hash


class TargetAllocation(TradingModel):
    instrument_id: UUID
    current_quantity: DecimalString
    target_quantity: DecimalString
    delta_quantity: DecimalString
    quantity_quantum: PositiveDecimal
    target_weight: SignedUnitDecimal
    constraint_diagnostics: tuple[str, ...] = Field(
        default_factory=tuple, max_length=64
    )

    @model_validator(mode="after")
    def validate_allocation(self) -> Self:
        if self.delta_quantity != self.target_quantity - self.current_quantity:
            raise ValueError("delta quantity must equal target minus current quantity")
        if not all(
            is_quantized(value, self.quantity_quantum)
            for value in (
                self.current_quantity,
                self.target_quantity,
                self.delta_quantity,
            )
        ):
            raise ValueError("allocation quantities must match instrument quantum")
        _require_sorted_unique(self.constraint_diagnostics, "constraint diagnostics")
        return self


class PortfolioTarget(TradingModel):
    target_id: UUID
    account_id: NonEmptyString
    portfolio_snapshot_id: UUID
    account_aggregate_sequence: int = Field(ge=0)
    portfolio_sequence: int = Field(ge=0)
    as_of: UTCDateTime
    allocations: tuple[TargetAllocation, ...] = Field(min_length=1, max_length=100_000)
    input_signal_ids: tuple[UUID, ...] = Field(max_length=100_000)
    policy_version: NonEmptyString
    policy_hash: Sha256
    expected_turnover: NonNegativeDecimal
    expected_cost: NonNegativeDecimal
    cost_currency: Currency
    calculation_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        target_id: UUID,
        account_id: str,
        portfolio_snapshot_id: UUID,
        account_aggregate_sequence: int,
        portfolio_sequence: int,
        as_of: datetime,
        allocations: tuple[TargetAllocation, ...],
        input_signal_ids: tuple[UUID, ...],
        policy_version: str,
        policy_hash: str,
        expected_turnover: Decimal,
        expected_cost: Decimal,
        cost_currency: str,
    ) -> PortfolioTarget:
        values = {
            "target_id": target_id,
            "account_id": account_id,
            "portfolio_snapshot_id": portfolio_snapshot_id,
            "account_aggregate_sequence": account_aggregate_sequence,
            "portfolio_sequence": portfolio_sequence,
            "as_of": as_of,
            "allocations": allocations,
            "input_signal_ids": input_signal_ids,
            "policy_version": policy_version,
            "policy_hash": policy_hash,
            "expected_turnover": expected_turnover,
            "expected_cost": expected_cost,
            "cost_currency": cost_currency,
        }
        provisional = cls.model_construct(
            target_id=target_id,
            account_id=account_id,
            portfolio_snapshot_id=portfolio_snapshot_id,
            account_aggregate_sequence=account_aggregate_sequence,
            portfolio_sequence=portfolio_sequence,
            as_of=as_of,
            allocations=allocations,
            input_signal_ids=input_signal_ids,
            policy_version=policy_version,
            policy_hash=policy_hash,
            expected_turnover=expected_turnover,
            expected_cost=expected_cost,
            cost_currency=cost_currency,
            calculation_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"calculation_hash": provisional.expected_calculation_hash()}
        )

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        _require_sorted_unique(
            tuple(str(item.instrument_id) for item in self.allocations),
            "target instruments",
        )
        _require_sorted_unique(
            tuple(str(item) for item in self.input_signal_ids), "input signal ids"
        )
        if self.calculation_hash != self.expected_calculation_hash():
            raise ValueError("portfolio calculation hash does not match payload")
        return self

    def expected_calculation_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(
                mode="json",
                exclude={"target_id", "calculation_hash"},
            )
        )


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique and stably sorted")
