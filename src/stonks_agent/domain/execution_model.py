"""Immutable inputs, policy, and outcome for deterministic paper execution."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel, is_quantized
from stonks_agent.domain.fills import ExecutionReceipt, Fill
from stonks_agent.domain.orders import (
    ExecutionCommand,
    OrderEvent,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from stonks_agent.domain.reservations import (
    AccountReservation,
    ReservationMutation,
    ReservationState,
)
from stonks_contracts.common import (
    Currency,
    DecimalString,
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    UnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64
_TERMINAL_ORDER_STATES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    }
)


class PaperExecutionPolicy(TradingModel):
    policy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    execution_model_version: str = Field(pattern=r"^paper-v[0-9]+$")
    model_kind: Literal["deterministic_next_bar"]
    realism_claim: Literal["reference_model_not_market_replay"]
    supported_order_types: tuple[OrderType, ...] = Field(min_length=1)
    supported_time_in_force: tuple[TimeInForce, ...] = Field(min_length=1)
    max_volume_participation: UnitDecimal
    half_spread_bps: NonNegativeDecimal
    base_slippage_bps: NonNegativeDecimal
    market_impact_bps_at_max_participation: NonNegativeDecimal
    fee_bps: NonNegativeDecimal
    per_unit_fee: NonNegativeDecimal
    minimum_fee: NonNegativeDecimal
    fee_quantum: PositiveDecimal

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        order_values = tuple(item.value for item in self.supported_order_types)
        tif_values = tuple(item.value for item in self.supported_time_in_force)
        if order_values != tuple(sorted(order_values)) or len(order_values) != len(
            set(order_values)
        ):
            raise ValueError("supported order types must be unique and sorted")
        if tif_values != tuple(sorted(tif_values)) or len(tif_values) != len(
            set(tif_values)
        ):
            raise ValueError("supported time-in-force values must be unique and sorted")
        if self.max_volume_participation <= 0:
            raise ValueError("volume participation must be positive")
        bps_values = (
            self.half_spread_bps,
            self.base_slippage_bps,
            self.market_impact_bps_at_max_participation,
            self.fee_bps,
        )
        if any(value > Decimal("10000") for value in bps_values):
            raise ValueError("execution basis points cannot exceed 10000")
        if not is_quantized(self.minimum_fee, self.fee_quantum):
            raise ValueError("minimum fee must match fee quantum")
        return self

    @property
    def policy_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


class ExecutionBar(TradingModel):
    instrument_id: UUID
    mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    opens_at: UTCDateTime
    closes_at: UTCDateTime
    available_at: UTCDateTime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal
    currency: Currency
    price_quantum: PositiveDecimal
    quantity_quantum: PositiveDecimal
    source_ref: NonEmptyString
    source_hash: Sha256
    tradable: bool

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        if not self.opens_at < self.closes_at <= self.available_at:
            raise ValueError("execution bar timeline is invalid")
        if self.high < self.low or not (
            self.low <= self.open <= self.high and self.low <= self.close <= self.high
        ):
            raise ValueError("execution bar OHLC is invalid")
        if not all(
            is_quantized(value, self.price_quantum)
            for value in (self.open, self.high, self.low, self.close)
        ):
            raise ValueError("execution bar prices must match price quantum")
        if not is_quantized(self.volume, self.quantity_quantum):
            raise ValueError("execution bar volume must match quantity quantum")
        return self


class PaperExecutionRequest(TradingModel):
    command: ExecutionCommand
    reservation: AccountReservation
    prior_events: tuple[OrderEvent, ...] = Field(max_length=100_000)
    prior_fills: tuple[Fill, ...] = Field(max_length=100_000)
    bars: tuple[ExecutionBar, ...] = Field(max_length=100_000)
    as_of: UTCDateTime

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        intent = self.command.intent
        reservation = self.reservation
        matches = (
            reservation.reservation_id == intent.reservation_id
            and reservation.order_intent_id == intent.intent_id
            and reservation.account_id == intent.account_id
            and reservation.instrument_id == intent.instrument_id
            and reservation.event_hash == self.command.reservation_hash
            and reservation.event_hash == intent.reservation_hash
            and reservation.account_aggregate_sequence
            == self.command.account_aggregate_sequence
        )
        if not matches:
            raise ValueError("execution reservation binding is stale")
        if reservation.state not in {
            ReservationState.OPEN,
            ReservationState.PARTIALLY_CONSUMED,
        }:
            raise ValueError("execution reservation is not active")
        if self.as_of < self.command.issued_at:
            raise ValueError("execution as-of precedes command")
        if self.prior_events:
            if self.prior_events[-1].to_status in _TERMINAL_ORDER_STATES:
                raise ValueError("terminal order cannot execute again")
            if any(
                item.order_intent_id != intent.intent_id for item in self.prior_events
            ):
                raise ValueError("prior order event belongs elsewhere")
        if any(
            item.command_id != self.command.command_id
            or item.order_intent_id != intent.intent_id
            for item in self.prior_fills
        ):
            raise ValueError("prior fill belongs elsewhere")
        return self


class PaperExecutionOutcome(TradingModel):
    receipt: ExecutionReceipt
    order_events: tuple[OrderEvent, ...] = Field(min_length=1, max_length=100_000)
    reservation_mutations: tuple[ReservationMutation, ...] = Field(max_length=100_000)
    final_reservation: AccountReservation
    reservation_consumed: DecimalString
    reservation_released: DecimalString
    input_hash: Sha256
    outcome_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        receipt: ExecutionReceipt,
        order_events: tuple[OrderEvent, ...],
        reservation_mutations: tuple[ReservationMutation, ...],
        final_reservation: AccountReservation,
        reservation_consumed: Decimal,
        reservation_released: Decimal,
        input_hash: str,
    ) -> PaperExecutionOutcome:
        values: dict[str, object] = {
            "receipt": receipt,
            "order_events": order_events,
            "reservation_mutations": reservation_mutations,
            "final_reservation": final_reservation,
            "reservation_consumed": reservation_consumed,
            "reservation_released": reservation_released,
            "input_hash": input_hash,
        }
        provisional = cls.model_construct(
            receipt=receipt,
            order_events=order_events,
            reservation_mutations=reservation_mutations,
            final_reservation=final_reservation,
            reservation_consumed=reservation_consumed,
            reservation_released=reservation_released,
            input_hash=input_hash,
            outcome_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"outcome_hash": provisional.expected_outcome_hash()}
        )

    @property
    def fill(self) -> Fill | None:
        return self.receipt.fills[-1] if self.receipt.fills else None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.receipt.event != self.order_events[-1]:
            raise ValueError("execution receipt must reference latest order event")
        if self.final_reservation.order_intent_id != self.receipt.event.order_intent_id:
            raise ValueError("execution reservation belongs elsewhere")
        if (
            self.reservation_mutations
            and self.reservation_mutations[-1].reservation != self.final_reservation
        ):
            raise ValueError("final reservation does not match mutation chain")
        if self.reservation_consumed < 0 or self.reservation_released < 0:
            raise ValueError("reservation accounting cannot be negative")
        if self.outcome_hash != self.expected_outcome_hash():
            raise ValueError("paper execution outcome hash does not match payload")
        return self

    def expected_outcome_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"outcome_hash"})
        )
