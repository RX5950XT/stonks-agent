"""Immutable canonical ledger head and replay-derived account projection."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel, is_quantized
from stonks_contracts.common import (
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64


class LedgerHead(TradingModel):
    account_id: NonEmptyString
    sequence: int = Field(ge=0)
    transaction_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_head(self) -> LedgerHead:
        if (self.sequence == 0) != (self.transaction_hash is None):
            raise ValueError("only genesis ledger head may omit transaction hash")
        return self


class LedgerAccountBalance(TradingModel):
    """Cumulative debit/credit totals for one canonical ledger account."""

    ledger_account: str = Field(
        pattern=r"^(asset|inventory|fee|pnl|clearing):[A-Za-z0-9_.:-]{1,191}$"
    )
    commodity: NonEmptyString
    quantum: PositiveDecimal
    debit_total: NonNegativeDecimal
    credit_total: NonNegativeDecimal

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if not is_quantized(self.debit_total, self.quantum) or not is_quantized(
            self.credit_total, self.quantum
        ):
            raise ValueError("ledger totals must match commodity quantum")
        return self

    @property
    def balance(self) -> Decimal:
        return self.debit_total - self.credit_total


class LedgerProjection(TradingModel):
    """Replay truth used to derive all settled trading projections."""

    account_id: NonEmptyString
    opening_snapshot_hash: Sha256
    ledger_sequence: int = Field(ge=0)
    ledger_hash: Sha256 | None = None
    last_occurred_at: UTCDateTime
    balances: tuple[LedgerAccountBalance, ...] = Field(max_length=1_000_000)
    unvalued_instrument_ids: tuple[UUID, ...] = Field(max_length=100_000)
    projection_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        opening_snapshot_hash: str,
        ledger_sequence: int,
        ledger_hash: str | None,
        last_occurred_at: datetime,
        balances: tuple[LedgerAccountBalance, ...],
        unvalued_instrument_ids: tuple[UUID, ...],
    ) -> LedgerProjection:
        values = {
            "account_id": account_id,
            "opening_snapshot_hash": opening_snapshot_hash,
            "ledger_sequence": ledger_sequence,
            "ledger_hash": ledger_hash,
            "last_occurred_at": last_occurred_at,
            "balances": balances,
            "unvalued_instrument_ids": unvalued_instrument_ids,
        }
        provisional = cls.model_construct(
            account_id=account_id,
            opening_snapshot_hash=opening_snapshot_hash,
            ledger_sequence=ledger_sequence,
            ledger_hash=ledger_hash,
            last_occurred_at=last_occurred_at,
            balances=balances,
            unvalued_instrument_ids=unvalued_instrument_ids,
            projection_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"projection_hash": provisional.expected_projection_hash()}
        )

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if (self.ledger_sequence == 0) != (self.ledger_hash is None):
            raise ValueError("only genesis ledger projection may omit hash")
        keys = tuple((item.ledger_account, item.commodity) for item in self.balances)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("ledger balances must be unique and stably sorted")
        identifiers = tuple(str(item) for item in self.unvalued_instrument_ids)
        if identifiers != tuple(sorted(identifiers)) or len(identifiers) != len(
            set(identifiers)
        ):
            raise ValueError("unvalued instruments must be unique and sorted")
        if self.projection_hash != self.expected_projection_hash():
            raise ValueError("ledger projection hash does not match payload")
        return self

    def account_balance(self, ledger_account: str, commodity: str) -> Decimal:
        for item in self.balances:
            if item.ledger_account == ledger_account and item.commodity == commodity:
                return item.balance
        return Decimal(0)

    def cash(self, currency: str) -> Decimal:
        return self.account_balance(f"asset:cash:{currency}", currency)

    def position(self, instrument_id: UUID) -> Decimal:
        commodity = str(instrument_id)
        return self.account_balance(f"inventory:units:{commodity}", commodity)

    def inventory_value(self, instrument_id: UUID, currency: str) -> Decimal:
        return self.account_balance(
            f"inventory:value:{instrument_id}:{currency}", currency
        )

    def fees(self, currency: str) -> Decimal:
        return self.account_balance(f"fee:execution:{currency}", currency)

    def realized_pnl(self, currency: str) -> Decimal:
        return -self.account_balance(f"pnl:realized:{currency}", currency)

    def expected_projection_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"projection_hash"})
        )


class LedgerReconciliationReport(TradingModel):
    account_id: NonEmptyString
    as_of: UTCDateTime
    ledger_sequence: int = Field(ge=0)
    replay_projection_hash: Sha256
    database_projection_hash: Sha256
    matched: bool
    mismatch_reasons: tuple[NonEmptyString, ...] = Field(max_length=1_000_000)

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.mismatch_reasons != tuple(sorted(set(self.mismatch_reasons))):
            raise ValueError("reconciliation reasons must be unique and sorted")
        if self.matched != (
            self.replay_projection_hash == self.database_projection_hash
            and not self.mismatch_reasons
        ):
            raise ValueError("reconciliation result is inconsistent")
        return self
