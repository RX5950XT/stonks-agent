"""Serialized account reservations and immutable reservation event transitions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel, failure, is_quantized
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.risk import RiskDecision
from stonks_contracts.common import (
    DecimalString,
    NonEmptyString,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)


class ReservationKind(StrEnum):
    CASH = "cash"
    POSITION = "position"


class ReservationState(StrEnum):
    OPEN = "open"
    PARTIALLY_CONSUMED = "partially_consumed"
    CONSUMED = "consumed"
    RELEASED = "released"
    EXPIRED = "expired"


class ReservationEventType(StrEnum):
    CREATED = "created"
    CONSUMED = "consumed"
    RELEASED = "released"
    EXPIRED = "expired"


class AccountReservation(TradingModel):
    reservation_id: UUID
    order_intent_id: UUID
    account_id: NonEmptyString
    instrument_id: UUID | None
    kind: ReservationKind
    commodity: NonEmptyString
    amount: PositiveDecimal
    remaining_amount: DecimalString
    quantum: PositiveDecimal
    risk_decision_id: UUID
    risk_decision_hash: Sha256
    portfolio_target_id: UUID
    authorized_target_hash: Sha256
    risk_account_aggregate_sequence: int = Field(ge=0)
    account_aggregate_sequence: int = Field(ge=1)
    portfolio_sequence: int = Field(ge=0)
    state: ReservationState
    created_at: UTCDateTime
    updated_at: UTCDateTime
    expires_at: UTCDateTime
    event_sequence: int = Field(ge=1)
    previous_event_hash: Sha256 | None = None
    event_hash: Sha256

    @model_validator(mode="after")
    def validate_reservation(self) -> Self:
        if self.account_aggregate_sequence != self.risk_account_aggregate_sequence + 1:
            raise ValueError("reservation must advance account aggregate sequence once")
        if self.updated_at < self.created_at:
            raise ValueError("reservation timeline is invalid")
        if self.state is ReservationState.EXPIRED:
            if self.updated_at < self.expires_at:
                raise ValueError("expired reservation event cannot precede expiry")
        elif self.updated_at >= self.expires_at:
            raise ValueError("non-expiry reservation event must precede expiry")
        if not all(
            is_quantized(value, self.quantum)
            for value in (self.amount, self.remaining_amount)
        ):
            raise ValueError("reservation amounts must match commodity quantum")
        if not Decimal("0") <= self.remaining_amount <= self.amount:
            raise ValueError("reservation remaining amount is outside valid range")
        _validate_state_amount(self.state, self.amount, self.remaining_amount)
        if (self.event_sequence == 1) != (self.previous_event_hash is None):
            raise ValueError("only genesis reservation event may omit previous hash")
        if self.kind is ReservationKind.POSITION and self.instrument_id is None:
            raise ValueError("position reservation requires an instrument")
        return self


class ReservationEvent(TradingModel):
    event_id: UUID
    reservation_id: UUID
    sequence: int = Field(ge=1)
    event_type: ReservationEventType
    from_state: ReservationState | None
    to_state: ReservationState
    amount: PositiveDecimal
    remaining_amount: DecimalString
    occurred_at: UTCDateTime
    reason: str = Field(min_length=1, max_length=256)
    previous_event_hash: Sha256 | None = None
    event_hash: Sha256

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if (self.sequence == 1) != (self.previous_event_hash is None):
            raise ValueError("only genesis reservation event may omit previous hash")
        if self.event_hash != self.expected_event_hash():
            raise ValueError("reservation event hash does not match payload")
        return self

    def expected_event_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json", exclude={"event_hash"}))


class ReservationMutation(TradingModel):
    reservation: AccountReservation
    event: ReservationEvent

    @model_validator(mode="after")
    def validate_mutation(self) -> Self:
        reservation = self.reservation
        event = self.event
        if (
            reservation.reservation_id != event.reservation_id
            or reservation.event_sequence != event.sequence
            or reservation.previous_event_hash != event.previous_event_hash
            or reservation.event_hash != event.event_hash
            or reservation.state is not event.to_state
            or reservation.remaining_amount != event.remaining_amount
            or reservation.updated_at != event.occurred_at
        ):
            raise ValueError("reservation projection does not match event")
        return self


def create_reservation(
    *,
    reservation_id: UUID,
    order_intent_id: UUID,
    decision: RiskDecision,
    kind: ReservationKind,
    commodity: str,
    amount: Decimal,
    quantum: Decimal,
    instrument_id: UUID | None,
    at: datetime,
    expires_at: datetime,
    current_account_sequence: int,
    current_portfolio_sequence: int,
) -> Result[ReservationMutation]:
    if not decision.is_current(
        account_aggregate_sequence=current_account_sequence,
        portfolio_sequence=current_portfolio_sequence,
        at=at,
    ):
        return failure(ErrorCode.CONFLICT, "risk decision is stale or not approved")
    try:
        outlives_decision = expires_at > decision.expires_at
    except TypeError:
        return failure(ErrorCode.INVALID_INPUT, "reservation time is invalid")
    if outlives_decision:
        return failure(
            ErrorCode.INVALID_INPUT, "reservation cannot outlive risk decision"
        )
    target_hash = decision.authorized_target_hash
    if target_hash is None:
        return failure(ErrorCode.CONFLICT, "risk decision has no authorized target")
    try:
        sequence = 1
        event = _event(
            reservation_id=reservation_id,
            sequence=sequence,
            event_type=ReservationEventType.CREATED,
            from_state=None,
            to_state=ReservationState.OPEN,
            amount=amount,
            remaining_amount=amount,
            occurred_at=at,
            reason="reservation_created",
            previous_event_hash=None,
        )
        reservation = AccountReservation(
            reservation_id=reservation_id,
            order_intent_id=order_intent_id,
            account_id=decision.account_id,
            instrument_id=instrument_id,
            kind=kind,
            commodity=commodity,
            amount=amount,
            remaining_amount=amount,
            quantum=quantum,
            risk_decision_id=decision.decision_id,
            risk_decision_hash=decision.decision_hash,
            portfolio_target_id=decision.portfolio_target_id,
            authorized_target_hash=target_hash,
            risk_account_aggregate_sequence=decision.account_aggregate_sequence,
            account_aggregate_sequence=current_account_sequence + 1,
            portfolio_sequence=current_portfolio_sequence,
            state=ReservationState.OPEN,
            created_at=at,
            updated_at=at,
            expires_at=expires_at,
            event_sequence=sequence,
            previous_event_hash=None,
            event_hash=event.event_hash,
        )
        return Success(ReservationMutation(reservation=reservation, event=event))
    except ValueError as error:
        return failure(
            ErrorCode.INVALID_INPUT, "reservation is invalid", reason=str(error)
        )


def consume_reservation(
    reservation: AccountReservation,
    *,
    amount: Decimal,
    at: datetime,
) -> Result[ReservationMutation]:
    invalid = _validate_mutable(reservation, at)
    if invalid is not None:
        return invalid
    if amount <= 0 or not is_quantized(amount, reservation.quantum):
        return failure(ErrorCode.INVALID_INPUT, "consume amount is invalid")
    if amount > reservation.remaining_amount:
        return failure(ErrorCode.CONFLICT, "consume amount exceeds reservation")
    remaining = reservation.remaining_amount - amount
    state = (
        ReservationState.CONSUMED
        if remaining == 0
        else ReservationState.PARTIALLY_CONSUMED
    )
    return _mutate(
        reservation,
        event_type=ReservationEventType.CONSUMED,
        target_state=state,
        amount=amount,
        remaining_amount=remaining,
        at=at,
        reason="reservation_consumed",
    )


def release_reservation(
    reservation: AccountReservation,
    *,
    at: datetime,
    reason: str,
) -> Result[ReservationMutation]:
    invalid = _validate_mutable(reservation, at)
    if invalid is not None:
        return invalid
    if not reason.strip():
        return failure(ErrorCode.INVALID_INPUT, "release reason is required")
    return _mutate(
        reservation,
        event_type=ReservationEventType.RELEASED,
        target_state=ReservationState.RELEASED,
        amount=reservation.remaining_amount,
        remaining_amount=Decimal("0"),
        at=at,
        reason=reason,
    )


def expire_reservation(
    reservation: AccountReservation,
    *,
    at: datetime,
) -> Result[ReservationMutation]:
    if reservation.state not in {
        ReservationState.OPEN,
        ReservationState.PARTIALLY_CONSUMED,
    }:
        return failure(ErrorCode.CONFLICT, "reservation is already terminal")
    try:
        not_due = at < reservation.expires_at or at < reservation.updated_at
    except TypeError:
        return failure(ErrorCode.INVALID_INPUT, "reservation event time is invalid")
    if not_due:
        return failure(ErrorCode.CONFLICT, "reservation is not eligible to expire")
    return _mutate(
        reservation,
        event_type=ReservationEventType.EXPIRED,
        target_state=ReservationState.EXPIRED,
        amount=reservation.remaining_amount,
        remaining_amount=Decimal("0"),
        at=at,
        reason="reservation_expired",
    )


def _mutate(
    reservation: AccountReservation,
    *,
    event_type: ReservationEventType,
    target_state: ReservationState,
    amount: Decimal,
    remaining_amount: Decimal,
    at: datetime,
    reason: str,
) -> Result[ReservationMutation]:
    event = _event(
        reservation_id=reservation.reservation_id,
        sequence=reservation.event_sequence + 1,
        event_type=event_type,
        from_state=reservation.state,
        to_state=target_state,
        amount=amount,
        remaining_amount=remaining_amount,
        occurred_at=at,
        reason=reason,
        previous_event_hash=reservation.event_hash,
    )
    values = reservation.model_dump()
    values.update(
        state=target_state,
        remaining_amount=remaining_amount,
        updated_at=at,
        event_sequence=event.sequence,
        previous_event_hash=event.previous_event_hash,
        event_hash=event.event_hash,
    )
    try:
        updated = AccountReservation.model_validate(values)
        return Success(ReservationMutation(reservation=updated, event=event))
    except ValueError as error:
        return failure(
            ErrorCode.CONFLICT, "reservation transition is invalid", reason=str(error)
        )


def _event(
    *,
    reservation_id: UUID,
    sequence: int,
    event_type: ReservationEventType,
    from_state: ReservationState | None,
    to_state: ReservationState,
    amount: Decimal,
    remaining_amount: Decimal,
    occurred_at: datetime,
    reason: str,
    previous_event_hash: str | None,
) -> ReservationEvent:
    event_id = uuid5(NAMESPACE_URL, f"reservation:{reservation_id}:{sequence}")
    values = {
        "event_id": event_id,
        "reservation_id": reservation_id,
        "sequence": sequence,
        "event_type": event_type,
        "from_state": from_state,
        "to_state": to_state,
        "amount": amount,
        "remaining_amount": remaining_amount,
        "occurred_at": occurred_at,
        "reason": reason,
        "previous_event_hash": previous_event_hash,
    }
    provisional = ReservationEvent.model_construct(
        event_id=event_id,
        reservation_id=reservation_id,
        sequence=sequence,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        amount=amount,
        remaining_amount=remaining_amount,
        occurred_at=occurred_at,
        reason=reason,
        previous_event_hash=previous_event_hash,
        event_hash="0" * 64,
    )
    return ReservationEvent.model_validate(
        values | {"event_hash": provisional.expected_event_hash()}
    )


def _validate_mutable(
    reservation: AccountReservation,
    at: datetime,
) -> Failure | None:
    if reservation.state not in {
        ReservationState.OPEN,
        ReservationState.PARTIALLY_CONSUMED,
    }:
        return failure(ErrorCode.CONFLICT, "reservation is already terminal")
    try:
        before_update = at < reservation.updated_at
        expired = at >= reservation.expires_at
    except TypeError:
        return failure(ErrorCode.INVALID_INPUT, "reservation event time is invalid")
    if before_update:
        return failure(ErrorCode.CONFLICT, "reservation event time moved backwards")
    if expired:
        return failure(ErrorCode.CONFLICT, "reservation is expired")
    return None


def _validate_state_amount(
    state: ReservationState,
    amount: Decimal,
    remaining: Decimal,
) -> None:
    valid = {
        ReservationState.OPEN: remaining == amount,
        ReservationState.PARTIALLY_CONSUMED: Decimal("0") < remaining < amount,
        ReservationState.CONSUMED: remaining == 0,
        ReservationState.RELEASED: remaining == 0,
        ReservationState.EXPIRED: remaining == 0,
    }
    if not valid[state]:
        raise ValueError("reservation state does not match remaining amount")
