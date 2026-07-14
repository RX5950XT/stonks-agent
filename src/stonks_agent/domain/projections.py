"""Read-only, content-hashed paper portfolio and risk projections."""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel, is_quantized
from stonks_agent.domain.portfolio import CashBalance, PositionBalance
from stonks_agent.domain.risk import RiskDecision
from stonks_contracts.common import (
    Currency,
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)
from stonks_contracts.report import ReportReference

_PLACEHOLDER_HASH = "0" * 64


class ProjectedCashBalance(TradingModel):
    currency: Currency
    settled_amount: NonNegativeDecimal
    reserved_amount: NonNegativeDecimal
    available_amount: NonNegativeDecimal
    quantum: PositiveDecimal

    @classmethod
    def from_balance(cls, balance: CashBalance) -> ProjectedCashBalance:
        return cls(
            currency=balance.currency,
            settled_amount=balance.settled_amount,
            reserved_amount=balance.reserved_amount,
            available_amount=balance.available_amount,
            quantum=balance.quantum,
        )

    @model_validator(mode="after")
    def validate_amounts(self) -> Self:
        if self.available_amount != self.settled_amount - self.reserved_amount:
            raise ValueError("projected available cash is inconsistent")
        if not all(
            is_quantized(value, self.quantum)
            for value in (
                self.settled_amount,
                self.reserved_amount,
                self.available_amount,
            )
        ):
            raise ValueError("projected cash must match its quantum")
        return self


class ProjectedPositionBalance(TradingModel):
    instrument_id: UUID
    quantity: NonNegativeDecimal
    sellable_quantity: NonNegativeDecimal
    reserved_quantity: NonNegativeDecimal
    available_quantity: NonNegativeDecimal
    quantum: PositiveDecimal

    @classmethod
    def from_balance(cls, balance: PositionBalance) -> ProjectedPositionBalance:
        return cls(
            instrument_id=balance.instrument_id,
            quantity=balance.quantity,
            sellable_quantity=balance.sellable_quantity,
            reserved_quantity=balance.reserved_quantity,
            available_quantity=balance.available_quantity,
            quantum=balance.quantum,
        )

    @model_validator(mode="after")
    def validate_quantities(self) -> Self:
        if self.available_quantity != self.sellable_quantity - self.reserved_quantity:
            raise ValueError("projected available position is inconsistent")
        if self.sellable_quantity > self.quantity:
            raise ValueError("projected sellable position exceeds quantity")
        if not all(
            is_quantized(value, self.quantum)
            for value in (
                self.quantity,
                self.sellable_quantity,
                self.reserved_quantity,
                self.available_quantity,
            )
        ):
            raise ValueError("projected position must match its quantum")
        return self


class PortfolioProjection(TradingModel):
    account_id: NonEmptyString
    base_currency: Currency
    as_of: UTCDateTime
    account_aggregate_sequence: int = Field(ge=0)
    portfolio_sequence: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    ledger_hash: Sha256 | None = None
    cash: tuple[ProjectedCashBalance, ...] = Field(max_length=64)
    positions: tuple[ProjectedPositionBalance, ...] = Field(max_length=100_000)
    pending_order_ids: tuple[UUID, ...] = Field(max_length=100_000)
    latest_target_ref: ReportReference | None = None
    projection_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        base_currency: str,
        as_of: datetime,
        account_aggregate_sequence: int,
        portfolio_sequence: int,
        ledger_sequence: int,
        ledger_hash: str | None,
        cash: tuple[ProjectedCashBalance, ...],
        positions: tuple[ProjectedPositionBalance, ...],
        pending_order_ids: tuple[UUID, ...],
        latest_target_ref: ReportReference | None,
    ) -> PortfolioProjection:
        values = {
            "account_id": account_id,
            "base_currency": base_currency,
            "as_of": as_of,
            "account_aggregate_sequence": account_aggregate_sequence,
            "portfolio_sequence": portfolio_sequence,
            "ledger_sequence": ledger_sequence,
            "ledger_hash": ledger_hash,
            "cash": cash,
            "positions": positions,
            "pending_order_ids": pending_order_ids,
            "latest_target_ref": latest_target_ref,
        }
        provisional = cls.model_construct(
            account_id=account_id,
            base_currency=base_currency,
            as_of=as_of,
            account_aggregate_sequence=account_aggregate_sequence,
            portfolio_sequence=portfolio_sequence,
            ledger_sequence=ledger_sequence,
            ledger_hash=ledger_hash,
            cash=cash,
            positions=positions,
            pending_order_ids=pending_order_ids,
            latest_target_ref=latest_target_ref,
            projection_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values
            | {
                "projection_hash": stable_payload_hash(
                    provisional.model_dump(mode="json", exclude={"projection_hash"})
                )
            }
        )

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if (self.ledger_sequence == 0) != (self.ledger_hash is None):
            raise ValueError("only genesis portfolio projection may omit ledger hash")
        _require_sorted_unique(tuple(item.currency for item in self.cash), "cash")
        _require_sorted_unique(
            tuple(str(item.instrument_id) for item in self.positions), "positions"
        )
        _require_sorted_unique(
            tuple(str(item) for item in self.pending_order_ids), "pending orders"
        )
        if self.projection_hash != self.expected_projection_hash():
            raise ValueError("portfolio projection hash does not match payload")
        return self

    def expected_projection_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"projection_hash"})
        )


class RiskProjection(TradingModel):
    account_id: NonEmptyString
    as_of: UTCDateTime
    observed_account_sequence: int = Field(ge=0)
    observed_portfolio_sequence: int = Field(ge=0)
    decision_account_sequence: int = Field(ge=0)
    decision_portfolio_sequence: int = Field(ge=0)
    decision_id: UUID
    decision_hash: Sha256
    portfolio_target_ref: ReportReference
    approved: bool
    currently_authorized: bool
    failed_checks: tuple[NonEmptyString, ...] = Field(max_length=256)
    policy_version: NonEmptyString
    policy_hash: Sha256
    decided_at: UTCDateTime
    expires_at: UTCDateTime
    projection_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        decision: RiskDecision,
        observed_account_sequence: int,
        observed_portfolio_sequence: int,
        as_of: datetime,
    ) -> RiskProjection:
        failed = tuple(
            sorted(
                f"{item.code}: {item.reason}"
                for item in decision.checks
                if not item.passed and item.reason is not None
            )
        )
        values = {
            "account_id": decision.account_id,
            "as_of": as_of,
            "observed_account_sequence": observed_account_sequence,
            "observed_portfolio_sequence": observed_portfolio_sequence,
            "decision_account_sequence": decision.account_aggregate_sequence,
            "decision_portfolio_sequence": decision.portfolio_sequence,
            "decision_id": decision.decision_id,
            "decision_hash": decision.decision_hash,
            "portfolio_target_ref": ReportReference(
                ref_id=decision.portfolio_target_id,
                content_hash=decision.input_target_hash,
            ),
            "approved": decision.approved,
            "currently_authorized": decision.is_current(
                account_aggregate_sequence=observed_account_sequence,
                portfolio_sequence=observed_portfolio_sequence,
                at=as_of,
            ),
            "failed_checks": failed,
            "policy_version": decision.policy_version,
            "policy_hash": decision.policy_hash,
            "decided_at": decision.decided_at,
            "expires_at": decision.expires_at,
        }
        provisional = cls.model_construct(
            **values,  # type: ignore[arg-type]
            projection_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values
            | {
                "projection_hash": stable_payload_hash(
                    provisional.model_dump(mode="json", exclude={"projection_hash"})
                )
            }
        )

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if self.as_of < self.decided_at:
            raise ValueError("risk projection cannot precede its decision")
        if self.failed_checks != tuple(sorted(set(self.failed_checks))):
            raise ValueError("failed risk checks must be unique and sorted")
        expected_current = (
            self.approved
            and self.decided_at <= self.as_of < self.expires_at
            and self.observed_account_sequence == self.decision_account_sequence
            and self.observed_portfolio_sequence == self.decision_portfolio_sequence
        )
        if self.currently_authorized != expected_current:
            raise ValueError("risk projection authority state is inconsistent")
        if self.projection_hash != self.expected_projection_hash():
            raise ValueError("risk projection hash does not match payload")
        return self

    def expected_projection_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"projection_hash"})
        )


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError(f"projected {label} must be unique and stably sorted")
