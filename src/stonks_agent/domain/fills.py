"""Canonical paper fills and immutable execution receipts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel, is_quantized
from stonks_agent.domain.orders import OrderEvent, OrderIntent, OrderSide, OrderStatus
from stonks_contracts.common import (
    Currency,
    DecimalString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64


class Fill(TradingModel):
    fill_id: UUID
    command_id: UUID
    order_intent_id: UUID
    account_id: str = Field(min_length=1)
    instrument_id: UUID
    side: OrderSide
    quantity: PositiveDecimal
    quantity_quantum: PositiveDecimal
    price: PositiveDecimal
    price_quantum: PositiveDecimal
    fee_currency: Currency
    fees: NonNegativeDecimal
    fee_quantum: PositiveDecimal = Decimal("0.01")
    slippage: DecimalString
    occurred_at: UTCDateTime
    simulator_ref: str | None = Field(default=None, max_length=512)
    external_ref: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_fill(self) -> Self:
        if not is_quantized(self.quantity, self.quantity_quantum):
            raise ValueError("fill quantity must match instrument quantum")
        if not is_quantized(self.price, self.price_quantum):
            raise ValueError("fill price must match price quantum")
        if not is_quantized(self.fees, self.fee_quantum):
            raise ValueError("fill fees must match fee currency quantum")
        if not is_quantized(self.slippage, self.price_quantum):
            raise ValueError("fill slippage must match price quantum")
        return self


class ExecutionReceipt(TradingModel):
    receipt_id: UUID
    command_id: UUID
    order_intent_id: UUID
    intent_hash: Sha256
    account_id: str = Field(min_length=1)
    instrument_id: UUID
    side: OrderSide
    command_quantity: PositiveDecimal
    quantity_quantum: PositiveDecimal
    status: OrderStatus
    event: OrderEvent
    fills: tuple[Fill, ...] = Field(default_factory=tuple, max_length=100_000)
    filled_quantity: NonNegativeDecimal
    remaining_quantity: NonNegativeDecimal
    occurred_at: UTCDateTime
    receipt_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        receipt_id: UUID,
        command_id: UUID,
        intent: OrderIntent,
        event: OrderEvent,
        fills: tuple[Fill, ...],
        occurred_at: datetime,
    ) -> ExecutionReceipt:
        values = {
            "receipt_id": receipt_id,
            "command_id": command_id,
            "order_intent_id": intent.intent_id,
            "intent_hash": intent.intent_hash,
            "account_id": intent.account_id,
            "instrument_id": intent.instrument_id,
            "side": intent.side,
            "command_quantity": intent.quantity,
            "quantity_quantum": intent.quantity_quantum,
            "status": event.to_status,
            "event": event,
            "fills": fills,
            "filled_quantity": event.cumulative_filled_quantity,
            "remaining_quantity": event.remaining_quantity,
            "occurred_at": occurred_at,
        }
        provisional = cls.model_construct(
            receipt_id=receipt_id,
            command_id=command_id,
            order_intent_id=intent.intent_id,
            intent_hash=intent.intent_hash,
            account_id=intent.account_id,
            instrument_id=intent.instrument_id,
            side=intent.side,
            command_quantity=intent.quantity,
            quantity_quantum=intent.quantity_quantum,
            status=event.to_status,
            event=event,
            fills=fills,
            filled_quantity=event.cumulative_filled_quantity,
            remaining_quantity=event.remaining_quantity,
            occurred_at=occurred_at,
            receipt_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"receipt_hash": provisional.expected_receipt_hash()}
        )

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if (
            self.order_intent_id != self.event.order_intent_id
            or self.status is not self.event.to_status
            or self.filled_quantity != self.event.cumulative_filled_quantity
            or self.remaining_quantity != self.event.remaining_quantity
        ):
            raise ValueError("execution receipt does not match order event")
        if self.filled_quantity + self.remaining_quantity != self.command_quantity:
            raise ValueError("receipt quantities must sum to command quantity")
        if not all(
            is_quantized(value, self.quantity_quantum)
            for value in (
                self.command_quantity,
                self.filled_quantity,
                self.remaining_quantity,
            )
        ):
            raise ValueError("receipt quantities must match instrument quantum")
        if self.occurred_at < self.event.occurred_at:
            raise ValueError("receipt cannot precede order event")
        fill_ids = tuple(str(item.fill_id) for item in self.fills)
        if fill_ids != tuple(sorted(fill_ids)) or len(fill_ids) != len(set(fill_ids)):
            raise ValueError("fills must be unique and stably sorted")
        if any(not self._fill_matches(item) for item in self.fills):
            raise ValueError("fill does not match receipt identity")
        if any(item.occurred_at > self.occurred_at for item in self.fills):
            raise ValueError("fill cannot occur after receipt")
        if (
            sum((item.quantity for item in self.fills), Decimal("0"))
            != self.filled_quantity
        ):
            raise ValueError("fill quantities must equal cumulative filled quantity")
        if self.status is OrderStatus.FILLED and not self.fills:
            raise ValueError("filled receipt requires at least one fill")
        if self.receipt_hash != self.expected_receipt_hash():
            raise ValueError("execution receipt hash does not match payload")
        return self

    def _fill_matches(self, fill: Fill) -> bool:
        return (
            fill.command_id == self.command_id
            and fill.order_intent_id == self.order_intent_id
            and fill.account_id == self.account_id
            and fill.instrument_id == self.instrument_id
            and fill.side is self.side
            and fill.quantity_quantum == self.quantity_quantum
        )

    def expected_receipt_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"receipt_hash"})
        )
