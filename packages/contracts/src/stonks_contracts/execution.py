"""Paper execution and balanced journal contracts."""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from .common import (
    ContractModel,
    Currency,
    DecimalString,
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
)

LEDGER_QUANTUM = Decimal("0.00000001")


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"


class OrderStatus(StrEnum):
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class OrderIntent(ContractModel):
    intent_id: UUID
    run_id: UUID
    account_id: NonEmptyString
    instrument_id: UUID
    side: OrderSide
    order_type: OrderType
    quantity: PositiveDecimal
    notional: PositiveDecimal | None = None
    limit_price: PositiveDecimal | None = None
    stop_price: PositiveDecimal | None = None
    time_in_force: TimeInForce
    valid_from: UTCDateTime
    valid_until: UTCDateTime
    risk_decision_id: UUID
    reservation_id: UUID
    portfolio_snapshot_id: UUID
    aggregate_sequence: int = Field(ge=0)
    idempotency_key: NonEmptyString
    execution_model_version: NonEmptyString
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_order_spec(self) -> Self:
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError("stop_price is required for stop orders")
        return self


class ExecutionCommand(ContractModel):
    command_id: UUID
    intent: OrderIntent
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString
    issued_at: UTCDateTime


class Fill(ContractModel):
    fill_id: UUID
    command_id: UUID
    order_intent_id: UUID
    account_id: NonEmptyString
    instrument_id: UUID
    side: OrderSide
    quantity: PositiveDecimal
    price: PositiveDecimal
    fee_currency: Currency
    fees: NonNegativeDecimal
    slippage: DecimalString
    occurred_at: UTCDateTime
    sequence: int = Field(ge=1)
    previous_event_hash: Sha256 | None = None
    simulator_ref: str | None = None
    external_ref: str | None = None

    @property
    def bar_time(self) -> UTCDateTime:
        """Alias the fill timestamp for next-bar paper execution assertions."""
        return self.occurred_at


class ExecutionReceipt(ContractModel):
    receipt_id: UUID
    command_id: UUID
    order_intent_id: UUID
    status: OrderStatus
    occurred_at: UTCDateTime
    sequence: int = Field(ge=1)
    previous_event_hash: Sha256 | None = None
    filled_quantity: NonNegativeDecimal
    remaining_quantity: NonNegativeDecimal
    command_quantity: PositiveDecimal
    fills: tuple[Fill, ...] = ()
    simulator_ref: str | None = None
    external_ref: str | None = None
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def validate_quantities(self) -> Self:
        if self.filled_quantity > self.command_quantity:
            raise ValueError("filled_quantity cannot exceed command quantity")
        if self.filled_quantity + self.remaining_quantity != self.command_quantity:
            raise ValueError("filled_quantity and remaining_quantity must sum to command_quantity")
        if self.status is OrderStatus.FILLED and self.remaining_quantity != 0:
            raise ValueError("filled receipt must have zero remaining_quantity")
        return self

    @property
    def fill(self) -> Fill | None:
        """Return the latest fill for the single-fill P0 paper executor."""
        return self.fills[-1] if self.fills else None


class JournalSide(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class JournalPosting(ContractModel):
    account: NonEmptyString
    commodity: NonEmptyString
    side: JournalSide
    amount: PositiveDecimal
    memo: str | None = None

    @property
    def signed_amount(self) -> Decimal:
        """Represent debits as positive and credits as negative."""
        return self.amount if self.side is JournalSide.DEBIT else -self.amount


class JournalTransaction(ContractModel):
    transaction_id: UUID
    sequence: int = Field(ge=1)
    occurred_at: UTCDateTime
    previous_hash: Sha256 | None = None
    source_fill_id: UUID
    source_order_intent_id: UUID | None = None
    postings: tuple[JournalPosting, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_balanced_postings(self) -> Self:
        balances: dict[str, Decimal] = defaultdict(Decimal)
        for posting in self.postings:
            signed = posting.amount if posting.side is JournalSide.DEBIT else -posting.amount
            balances[posting.commodity] += signed
        unbalanced = [
            commodity
            for commodity, balance in balances.items()
            if balance.quantize(LEDGER_QUANTUM, rounding=ROUND_HALF_EVEN) != 0
        ]
        if unbalanced:
            raise ValueError(f"unbalanced journal commodities: {', '.join(sorted(unbalanced))}")
        return self

    def is_balanced(self) -> bool:
        """Report balance per commodity using the canonical ledger quantum."""
        balances: dict[str, Decimal] = defaultdict(Decimal)
        for posting in self.postings:
            balances[posting.commodity] += posting.signed_amount
        return all(
            balance.quantize(LEDGER_QUANTUM, rounding=ROUND_HALF_EVEN) == 0
            for balance in balances.values()
        )
