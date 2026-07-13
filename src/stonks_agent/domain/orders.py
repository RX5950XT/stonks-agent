"""Reservation-backed paper order intents, commands, and hash-chained events."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel, failure, is_quantized
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.reservations import (
    AccountReservation,
    ReservationKind,
    ReservationState,
)
from stonks_agent.domain.risk import RiskDecision
from stonks_contracts.common import (
    DecimalString,
    NonEmptyString,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64


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
    CREATED = "created"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {
            OrderStatus.ACCEPTED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.ACCEPTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}


class OrderIntent(TradingModel):
    intent_id: UUID
    run_id: UUID
    account_id: NonEmptyString
    instrument_id: UUID
    side: OrderSide
    order_type: OrderType
    quantity: PositiveDecimal
    quantity_quantum: PositiveDecimal
    limit_price: PositiveDecimal | None = None
    stop_price: PositiveDecimal | None = None
    time_in_force: TimeInForce
    valid_from: UTCDateTime
    valid_until: UTCDateTime
    risk_decision_id: UUID
    risk_decision_hash: Sha256
    reservation_id: UUID
    reservation_hash: Sha256
    portfolio_target_id: UUID
    authorized_target_hash: Sha256
    portfolio_snapshot_id: UUID
    risk_account_aggregate_sequence: int = Field(ge=0)
    account_aggregate_sequence: int = Field(ge=1)
    portfolio_sequence: int = Field(ge=0)
    idempotency_key: NonEmptyString
    execution_model_version: NonEmptyString
    created_at: UTCDateTime
    intent_hash: Sha256

    @model_validator(mode="after")
    def validate_intent(self) -> Self:
        if not self.created_at <= self.valid_from < self.valid_until:
            raise ValueError("order validity timeline is invalid")
        if not is_quantized(self.quantity, self.quantity_quantum):
            raise ValueError("order quantity must match instrument quantum")
        if (
            self.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}
            and self.limit_price is None
        ):
            raise ValueError("limit order requires limit price")
        if (
            self.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}
            and self.stop_price is None
        ):
            raise ValueError("stop order requires stop price")
        if self.intent_hash != self.expected_intent_hash():
            raise ValueError("order intent hash does not match payload")
        return self

    def expected_intent_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"intent_hash"})
        )


class ExecutionCommand(TradingModel):
    command_id: UUID
    intent: OrderIntent
    risk_decision_hash: Sha256
    reservation_hash: Sha256
    account_aggregate_sequence: int = Field(ge=1)
    portfolio_sequence: int = Field(ge=0)
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString
    issued_at: UTCDateTime
    command_hash: Sha256

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if self.issued_at < self.intent.created_at:
            raise ValueError("execution command cannot precede order intent")
        if (
            self.risk_decision_hash != self.intent.risk_decision_hash
            or self.reservation_hash != self.intent.reservation_hash
            or self.account_aggregate_sequence != self.intent.account_aggregate_sequence
            or self.portfolio_sequence != self.intent.portfolio_sequence
        ):
            raise ValueError("execution command binding does not match intent")
        if self.command_hash != self.expected_command_hash():
            raise ValueError("execution command hash does not match payload")
        return self

    def expected_command_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"attempt_nonce", "command_hash"})
        )


class OrderEvent(TradingModel):
    event_id: UUID
    order_intent_id: UUID
    sequence: int = Field(ge=1)
    from_status: OrderStatus
    to_status: OrderStatus
    cumulative_filled_quantity: DecimalString
    remaining_quantity: DecimalString
    occurred_at: UTCDateTime
    reason: str | None = Field(default=None, max_length=256)
    previous_event_hash: Sha256 | None = None
    event_hash: Sha256

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.to_status not in _TRANSITIONS[self.from_status]:
            raise ValueError("order status transition is not allowed")
        if (self.sequence == 1) != (self.previous_event_hash is None):
            raise ValueError("only genesis order event may omit previous hash")
        if self.event_hash != self.expected_event_hash():
            raise ValueError("order event hash does not match payload")
        return self

    def expected_event_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json", exclude={"event_hash"}))


def create_order_intent(
    *,
    intent_id: UUID,
    run_id: UUID,
    decision: RiskDecision,
    reservation: AccountReservation,
    instrument_id: UUID,
    side: OrderSide,
    order_type: OrderType,
    quantity: Decimal,
    quantity_quantum: Decimal,
    limit_price: Decimal | None,
    stop_price: Decimal | None,
    time_in_force: TimeInForce,
    valid_from: datetime,
    valid_until: datetime,
    idempotency_key: str,
    execution_model_version: str,
    created_at: datetime,
) -> Result[OrderIntent]:
    invalid = _validate_intent_binding(
        intent_id,
        decision,
        reservation,
        instrument_id,
        side,
        quantity,
        quantity_quantum,
        valid_until,
        created_at,
    )
    if invalid is not None:
        return invalid
    normalized = decision.normalized_target
    if normalized is None:
        return failure(ErrorCode.CONFLICT, "risk decision has no authorized target")
    try:
        provisional = OrderIntent.model_construct(
            intent_id=intent_id,
            run_id=run_id,
            account_id=decision.account_id,
            instrument_id=instrument_id,
            side=side,
            order_type=order_type,
            quantity=quantity,
            quantity_quantum=quantity_quantum,
            limit_price=limit_price,
            stop_price=stop_price,
            time_in_force=time_in_force,
            valid_from=valid_from,
            valid_until=valid_until,
            risk_decision_id=decision.decision_id,
            risk_decision_hash=decision.decision_hash,
            reservation_id=reservation.reservation_id,
            reservation_hash=reservation.event_hash,
            portfolio_target_id=decision.portfolio_target_id,
            authorized_target_hash=reservation.authorized_target_hash,
            portfolio_snapshot_id=normalized.portfolio_snapshot_id,
            risk_account_aggregate_sequence=decision.account_aggregate_sequence,
            account_aggregate_sequence=reservation.account_aggregate_sequence,
            portfolio_sequence=decision.portfolio_sequence,
            idempotency_key=idempotency_key,
            execution_model_version=execution_model_version,
            created_at=created_at,
            intent_hash=_PLACEHOLDER_HASH,
        )
        values = provisional.model_dump(exclude={"intent_hash"})
        return Success(
            OrderIntent(
                **values,
                intent_hash=provisional.expected_intent_hash(),
            )
        )
    except ValueError as error:
        return failure(
            ErrorCode.INVALID_INPUT, "order intent is invalid", reason=str(error)
        )


def build_execution_command(
    *,
    command_id: UUID,
    intent: OrderIntent,
    decision: RiskDecision,
    reservation: AccountReservation | None,
    current_account_sequence: int,
    current_portfolio_sequence: int,
    attempt_generation: int,
    attempt_nonce: str,
    issued_at: datetime,
) -> Result[ExecutionCommand]:
    invalid = _validate_command_binding(
        intent,
        decision,
        reservation,
        current_account_sequence,
        current_portfolio_sequence,
        issued_at,
    )
    if invalid is not None:
        return invalid
    assert reservation is not None
    try:
        values = {
            "command_id": command_id,
            "intent": intent,
            "risk_decision_hash": decision.decision_hash,
            "reservation_hash": reservation.event_hash,
            "account_aggregate_sequence": current_account_sequence,
            "portfolio_sequence": current_portfolio_sequence,
            "attempt_generation": attempt_generation,
            "attempt_nonce": attempt_nonce,
            "issued_at": issued_at,
        }
        provisional = ExecutionCommand.model_construct(
            command_id=command_id,
            intent=intent,
            risk_decision_hash=decision.decision_hash,
            reservation_hash=reservation.event_hash,
            account_aggregate_sequence=current_account_sequence,
            portfolio_sequence=current_portfolio_sequence,
            attempt_generation=attempt_generation,
            attempt_nonce=attempt_nonce,
            issued_at=issued_at,
            command_hash=_PLACEHOLDER_HASH,
        )
        return Success(
            ExecutionCommand.model_validate(
                values | {"command_hash": provisional.expected_command_hash()}
            )
        )
    except ValueError as error:
        return failure(
            ErrorCode.INVALID_INPUT, "execution command is invalid", reason=str(error)
        )


def append_order_event(
    intent: OrderIntent,
    *,
    previous: OrderEvent | None,
    target_status: OrderStatus,
    cumulative_filled_quantity: Decimal,
    occurred_at: datetime,
    reason: str | None = None,
) -> Result[OrderEvent]:
    from_status = previous.to_status if previous is not None else OrderStatus.CREATED
    try:
        before_intent = occurred_at < intent.created_at
        outside_validity = (
            occurred_at < intent.valid_until
            if target_status is OrderStatus.EXPIRED
            else occurred_at >= intent.valid_until
        )
    except TypeError:
        return failure(ErrorCode.INVALID_INPUT, "order event time is invalid")
    if before_intent:
        return failure(ErrorCode.CONFLICT, "order event cannot precede intent")
    if outside_validity:
        return failure(ErrorCode.CONFLICT, "order event violates validity window")
    if target_status not in _TRANSITIONS[from_status]:
        return failure(ErrorCode.CONFLICT, "order status transition is not allowed")
    if previous is not None:
        if previous.order_intent_id != intent.intent_id:
            return failure(ErrorCode.CONFLICT, "previous order event belongs elsewhere")
        if occurred_at < previous.occurred_at:
            return failure(ErrorCode.CONFLICT, "order event time moved backwards")
        if cumulative_filled_quantity < previous.cumulative_filled_quantity:
            return failure(ErrorCode.CONFLICT, "filled quantity cannot decrease")
    if not is_quantized(cumulative_filled_quantity, intent.quantity_quantum):
        return failure(ErrorCode.INVALID_INPUT, "filled quantity is not quantized")
    remaining = intent.quantity - cumulative_filled_quantity
    if cumulative_filled_quantity < 0 or remaining < 0:
        return failure(ErrorCode.CONFLICT, "order event would overfill intent")
    invalid = _validate_status_quantities(
        target_status, cumulative_filled_quantity, remaining
    )
    if invalid is not None:
        return invalid
    sequence = previous.sequence + 1 if previous is not None else 1
    event_id = uuid5(NAMESPACE_URL, f"order:{intent.intent_id}:{sequence}")
    values = {
        "event_id": event_id,
        "order_intent_id": intent.intent_id,
        "sequence": sequence,
        "from_status": from_status,
        "to_status": target_status,
        "cumulative_filled_quantity": cumulative_filled_quantity,
        "remaining_quantity": remaining,
        "occurred_at": occurred_at,
        "reason": reason,
        "previous_event_hash": previous.event_hash if previous is not None else None,
    }
    provisional = OrderEvent.model_construct(
        event_id=event_id,
        order_intent_id=intent.intent_id,
        sequence=sequence,
        from_status=from_status,
        to_status=target_status,
        cumulative_filled_quantity=cumulative_filled_quantity,
        remaining_quantity=remaining,
        occurred_at=occurred_at,
        reason=reason,
        previous_event_hash=previous.event_hash if previous is not None else None,
        event_hash=_PLACEHOLDER_HASH,
    )
    try:
        return Success(
            OrderEvent.model_validate(
                values | {"event_hash": provisional.expected_event_hash()}
            )
        )
    except ValueError as error:
        return failure(
            ErrorCode.INVALID_INPUT, "order event is invalid", reason=str(error)
        )


def _validate_intent_binding(
    intent_id: UUID,
    decision: RiskDecision,
    reservation: AccountReservation,
    instrument_id: UUID,
    side: OrderSide,
    quantity: Decimal,
    quantity_quantum: Decimal,
    valid_until: datetime,
    created_at: datetime,
) -> Failure | None:
    target_hash = decision.authorized_target_hash
    if not decision.approved or target_hash is None:
        return failure(ErrorCode.CONFLICT, "risk decision is not approved")
    if reservation.state is not ReservationState.OPEN:
        return failure(ErrorCode.CONFLICT, "reservation is not open")
    matches = (
        reservation.order_intent_id == intent_id
        and reservation.risk_decision_id == decision.decision_id
        and reservation.risk_decision_hash == decision.decision_hash
        and reservation.authorized_target_hash == target_hash
        and reservation.account_id == decision.account_id
        and reservation.instrument_id == instrument_id
        and reservation.risk_account_aggregate_sequence
        == decision.account_aggregate_sequence
        and reservation.portfolio_sequence == decision.portfolio_sequence
    )
    if not matches:
        return failure(ErrorCode.CONFLICT, "risk and reservation binding mismatch")
    target = decision.normalized_target
    assert target is not None
    allocation = next(
        (item for item in target.allocations if item.instrument_id == instrument_id),
        None,
    )
    expected_side = (
        OrderSide.BUY
        if allocation is not None and allocation.delta_quantity > 0
        else OrderSide.SELL
    )
    expected_kind = (
        ReservationKind.CASH
        if expected_side is OrderSide.BUY
        else ReservationKind.POSITION
    )
    if (
        allocation is None
        or allocation.delta_quantity == 0
        or quantity != abs(allocation.delta_quantity)
        or quantity_quantum != allocation.quantity_quantum
        or side is not expected_side
        or reservation.kind is not expected_kind
    ):
        return failure(
            ErrorCode.CONFLICT, "order does not match authorized target delta"
        )
    try:
        invalid_timeline = (
            created_at < decision.decided_at or valid_until > reservation.expires_at
        )
    except TypeError:
        return failure(ErrorCode.INVALID_INPUT, "order authorization time is invalid")
    if invalid_timeline:
        return failure(ErrorCode.CONFLICT, "order validity exceeds authorization")
    return None


def _validate_command_binding(
    intent: OrderIntent,
    decision: RiskDecision,
    reservation: AccountReservation | None,
    current_account_sequence: int,
    current_portfolio_sequence: int,
    issued_at: datetime,
) -> Failure | None:
    if reservation is None:
        return failure(ErrorCode.CONFLICT, "open reservation is required")
    matches = (
        decision.approved
        and intent.risk_decision_id == decision.decision_id
        and intent.risk_decision_hash == decision.decision_hash
        and intent.reservation_id == reservation.reservation_id
        and intent.reservation_hash == reservation.event_hash
        and reservation.state is ReservationState.OPEN
        and reservation.order_intent_id == intent.intent_id
        and current_account_sequence == intent.account_aggregate_sequence
        and current_account_sequence == reservation.account_aggregate_sequence
        and current_portfolio_sequence == intent.portfolio_sequence
        and current_portfolio_sequence == decision.portfolio_sequence
    )
    if not matches:
        return failure(ErrorCode.CONFLICT, "execution authorization binding is stale")
    try:
        outside_window = not intent.created_at <= issued_at < intent.valid_until
        expired = (
            issued_at >= reservation.expires_at or issued_at >= decision.expires_at
        )
    except TypeError:
        return failure(ErrorCode.INVALID_INPUT, "execution command time is invalid")
    if outside_window:
        return failure(
            ErrorCode.CONFLICT, "execution command is outside validity window"
        )
    if expired:
        return failure(ErrorCode.CONFLICT, "execution authorization expired")
    return None


def _validate_status_quantities(
    status: OrderStatus,
    filled: Decimal,
    remaining: Decimal,
) -> Failure | None:
    if status is OrderStatus.ACCEPTED and filled != 0:
        return failure(ErrorCode.CONFLICT, "accepted order cannot already be filled")
    if status is OrderStatus.PARTIALLY_FILLED and not (filled > 0 and remaining > 0):
        return failure(ErrorCode.CONFLICT, "partial fill quantities are invalid")
    if status is OrderStatus.FILLED and remaining != 0:
        return failure(ErrorCode.CONFLICT, "filled order must have zero remaining")
    return None
