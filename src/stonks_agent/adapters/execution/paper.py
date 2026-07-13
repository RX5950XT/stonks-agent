"""Deterministic, next-tradable-bar reference paper broker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml

from stonks_agent.domain._trading import failure, is_quantized
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.execution_model import (
    ExecutionBar,
    PaperExecutionOutcome,
    PaperExecutionPolicy,
    PaperExecutionRequest,
)
from stonks_agent.domain.fills import ExecutionReceipt, Fill
from stonks_agent.domain.orders import (
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    append_order_event,
)
from stonks_agent.domain.reservations import (
    AccountReservation,
    ReservationKind,
    ReservationMutation,
    consume_reservation,
    expire_reservation,
    release_reservation,
)
from stonks_contracts.common import stable_payload_hash

ZERO = Decimal(0)
BPS = Decimal("10000")


def load_paper_execution_policy(path: str | Path) -> PaperExecutionPolicy:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return PaperExecutionPolicy.model_validate(payload)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise ValueError("paper execution policy could not be loaded") from error


@dataclass(frozen=True, slots=True)
class ReferencePaperBroker:
    policy: PaperExecutionPolicy

    def execute(self, request: PaperExecutionRequest) -> Result[PaperExecutionOutcome]:
        invalid = _request_error(request, self.policy)
        if invalid is not None:
            return invalid
        if request.command.intent.order_type not in self.policy.supported_order_types:
            return _terminal_without_fill(
                request,
                self.policy,
                status=OrderStatus.REJECTED,
                reason="unsupported_order_type",
                occurred_at=request.command.issued_at,
            )
        if (
            request.command.intent.time_in_force
            not in self.policy.supported_time_in_force
        ):
            return _terminal_without_fill(
                request,
                self.policy,
                status=OrderStatus.REJECTED,
                reason="unsupported_time_in_force",
                occurred_at=request.command.issued_at,
            )
        events = list(request.prior_events)
        if not events:
            accepted = append_order_event(
                request.command.intent,
                previous=None,
                target_status=OrderStatus.ACCEPTED,
                cumulative_filled_quantity=ZERO,
                occurred_at=request.command.issued_at,
                reason="paper_command_accepted",
            )
            if isinstance(accepted, Failure):
                return accepted
            events.append(accepted.value)
        bar = _next_bar(request)
        if bar is None:
            return _pending_or_expired(request, self.policy, events)
        price = _fill_price(request, bar, self.policy)
        if price is None:
            return _pending_or_expired(request, self.policy, events)
        quantity = _fill_quantity(request, bar, price, self.policy)
        if quantity <= 0:
            return _terminal_without_fill(
                request,
                self.policy,
                status=OrderStatus.REJECTED,
                reason="reservation_insufficient",
                occurred_at=request.command.issued_at,
            )
        return _filled_outcome(request, self.policy, events, bar, price, quantity)


def _request_error(
    request: PaperExecutionRequest,
    policy: PaperExecutionPolicy,
) -> Failure | None:
    intent = request.command.intent
    if intent.execution_model_version != policy.execution_model_version:
        return failure(ErrorCode.CONFLICT, "Execution model version does not match")
    opens = tuple(item.opens_at for item in request.bars)
    if opens != tuple(sorted(opens)) or len(opens) != len(set(opens)):
        return failure(
            ErrorCode.INVALID_INPUT, "Execution bars must be unique and sorted"
        )
    if any(item.available_at > request.as_of for item in request.bars):
        return failure(ErrorCode.INVALID_INPUT, "Execution bar is unavailable at as-of")
    if any(item.instrument_id != intent.instrument_id for item in request.bars):
        return failure(ErrorCode.CONFLICT, "Execution bar instrument does not match")
    if any(item.quantity_quantum != intent.quantity_quantum for item in request.bars):
        return failure(ErrorCode.CONFLICT, "Execution bar quantity quantum differs")
    event_error = _event_chain_error(request)
    if event_error is not None:
        return event_error
    return None


def _event_chain_error(request: PaperExecutionRequest) -> Failure | None:
    previous_hash: str | None = None
    previous_status = OrderStatus.CREATED
    cumulative = ZERO
    for sequence, event in enumerate(request.prior_events, start=1):
        valid = (
            event.sequence == sequence
            and event.previous_event_hash == previous_hash
            and event.from_status is previous_status
            and event.cumulative_filled_quantity >= cumulative
        )
        if not valid:
            return failure(ErrorCode.CONFLICT, "Prior order event chain is invalid")
        previous_hash = event.event_hash
        previous_status = event.to_status
        cumulative = event.cumulative_filled_quantity
    fill_total = sum((item.quantity for item in request.prior_fills), ZERO)
    if fill_total != cumulative:
        return failure(ErrorCode.CONFLICT, "Prior fills do not match order events")
    return None


def _next_bar(request: PaperExecutionRequest) -> ExecutionBar | None:
    last_time = (
        request.prior_events[-1].occurred_at
        if request.prior_events
        else request.command.issued_at
    )
    return next(
        (
            item
            for item in request.bars
            if item.tradable
            and item.opens_at > last_time
            and item.opens_at < request.command.intent.valid_until
        ),
        None,
    )


def _fill_price(
    request: PaperExecutionRequest,
    bar: ExecutionBar,
    policy: PaperExecutionPolicy,
) -> Decimal | None:
    intent = request.command.intent
    participation = min(
        policy.max_volume_participation,
        intent.quantity / bar.volume if bar.volume else ZERO,
    )
    impact = (
        policy.market_impact_bps_at_max_participation
        * participation
        / policy.max_volume_participation
    )
    adverse_bps = policy.half_spread_bps + policy.base_slippage_bps + impact
    direction = Decimal(1) if intent.side is OrderSide.BUY else Decimal(-1)
    adjusted = bar.open * (Decimal(1) + direction * adverse_bps / BPS)
    adjusted = _adverse_quantize(adjusted, bar.price_quantum, intent.side)
    if intent.order_type is OrderType.MARKET:
        return adjusted
    if intent.order_type is not OrderType.LIMIT or intent.limit_price is None:
        return None
    if not is_quantized(intent.limit_price, bar.price_quantum):
        return None
    if intent.side is OrderSide.BUY:
        if bar.open <= intent.limit_price:
            return min(adjusted, intent.limit_price)
        return intent.limit_price if bar.low <= intent.limit_price else None
    if bar.open >= intent.limit_price:
        return max(adjusted, intent.limit_price)
    return intent.limit_price if bar.high >= intent.limit_price else None


def _fill_quantity(
    request: PaperExecutionRequest,
    bar: ExecutionBar,
    price: Decimal,
    policy: PaperExecutionPolicy,
) -> Decimal:
    intent = request.command.intent
    already_filled = (
        request.prior_events[-1].cumulative_filled_quantity
        if request.prior_events
        else ZERO
    )
    remaining = intent.quantity - already_filled
    volume_cap = _floor_quantum(
        bar.volume * policy.max_volume_participation,
        intent.quantity_quantum,
    )
    quantity = min(remaining, volume_cap)
    if request.reservation.kind is ReservationKind.POSITION:
        return min(quantity, request.reservation.remaining_amount)
    return _affordable_quantity(
        quantity,
        price,
        intent.quantity_quantum,
        request.reservation.remaining_amount,
        policy,
    )


def _affordable_quantity(
    quantity: Decimal,
    price: Decimal,
    quantum: Decimal,
    available: Decimal,
    policy: PaperExecutionPolicy,
) -> Decimal:
    approximate_unit = price * (Decimal(1) + policy.fee_bps / BPS) + policy.per_unit_fee
    conservative = max(ZERO, available - policy.minimum_fee)
    affordable = _floor_quantum(conservative / approximate_unit, quantum)
    candidate = min(quantity, affordable)
    while candidate > 0 and _cash_consumption(candidate, price, policy) > available:
        candidate -= quantum
    return max(ZERO, candidate)


def _filled_outcome(
    request: PaperExecutionRequest,
    policy: PaperExecutionPolicy,
    events: list[OrderEvent],
    bar: ExecutionBar,
    price: Decimal,
    quantity: Decimal,
) -> Result[PaperExecutionOutcome]:
    intent = request.command.intent
    cumulative = events[-1].cumulative_filled_quantity + quantity
    status = (
        OrderStatus.FILLED
        if cumulative == intent.quantity
        else OrderStatus.PARTIALLY_FILLED
    )
    event = append_order_event(
        intent,
        previous=events[-1],
        target_status=status,
        cumulative_filled_quantity=cumulative,
        occurred_at=bar.opens_at,
        reason="paper_next_bar_fill",
    )
    if isinstance(event, Failure):
        return event
    events.append(event.value)
    fee = _fee(quantity, price, policy)
    fill = _fill(request, policy, bar, quantity, price, fee, event.value.sequence)
    fills = tuple(
        sorted((*request.prior_fills, fill), key=lambda item: str(item.fill_id))
    )
    mutations, final, consumed, released = _reservation_after_fill(
        request.reservation,
        quantity=quantity,
        price=price,
        fee=fee,
        at=bar.opens_at,
        terminal=status is OrderStatus.FILLED,
    )
    if (
        intent.time_in_force is TimeInForce.IOC
        and status is OrderStatus.PARTIALLY_FILLED
    ):
        cancelled = append_order_event(
            intent,
            previous=events[-1],
            target_status=OrderStatus.CANCELLED,
            cumulative_filled_quantity=cumulative,
            occurred_at=bar.opens_at,
            reason="ioc_remainder_cancelled",
        )
        if isinstance(cancelled, Failure):
            return cancelled
        events.append(cancelled.value)
        terminal_mutations, final, terminal_release = _release_remaining(
            final,
            at=bar.opens_at,
            reason="ioc_remainder_released",
        )
        mutations = (*mutations, *terminal_mutations)
        released += terminal_release
    return _outcome(
        request, policy, tuple(events), fills, mutations, final, consumed, released
    )


def _pending_or_expired(
    request: PaperExecutionRequest,
    policy: PaperExecutionPolicy,
    events: list[OrderEvent],
) -> Result[PaperExecutionOutcome]:
    if request.as_of < request.command.intent.valid_until:
        return _outcome(
            request,
            policy,
            tuple(events),
            request.prior_fills,
            (),
            request.reservation,
            ZERO,
            ZERO,
        )
    expired = append_order_event(
        request.command.intent,
        previous=events[-1],
        target_status=OrderStatus.EXPIRED,
        cumulative_filled_quantity=events[-1].cumulative_filled_quantity,
        occurred_at=request.command.intent.valid_until,
        reason="order_validity_expired",
    )
    if isinstance(expired, Failure):
        return expired
    events.append(expired.value)
    mutations, final, released = _release_remaining(
        request.reservation,
        at=request.command.intent.valid_until,
        reason="expired_order_released",
    )
    return _outcome(
        request,
        policy,
        tuple(events),
        request.prior_fills,
        mutations,
        final,
        ZERO,
        released,
    )


def _terminal_without_fill(
    request: PaperExecutionRequest,
    policy: PaperExecutionPolicy,
    *,
    status: OrderStatus,
    reason: str,
    occurred_at: datetime,
) -> Result[PaperExecutionOutcome]:
    event = append_order_event(
        request.command.intent,
        previous=None,
        target_status=status,
        cumulative_filled_quantity=ZERO,
        occurred_at=occurred_at,
        reason=reason,
    )
    if isinstance(event, Failure):
        return event
    mutations, final, released = _release_remaining(
        request.reservation,
        at=event.value.occurred_at,
        reason=f"{reason}_released",
    )
    return _outcome(
        request,
        policy,
        (event.value,),
        (),
        mutations,
        final,
        ZERO,
        released,
    )


def _reservation_after_fill(
    reservation: AccountReservation,
    *,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    at: datetime,
    terminal: bool,
) -> tuple[tuple[ReservationMutation, ...], AccountReservation, Decimal, Decimal]:
    amount = (
        _ceil_quantum(quantity * price + fee, reservation.quantum)
        if reservation.kind is ReservationKind.CASH
        else quantity
    )
    consumed = consume_reservation(reservation, amount=amount, at=at)
    if isinstance(consumed, Failure):
        raise ValueError("validated fill could not consume reservation")
    mutations: tuple[ReservationMutation, ...] = (consumed.value,)
    final = consumed.value.reservation
    released = ZERO
    if terminal and final.remaining_amount > 0:
        extra, final, released = _release_remaining(
            final,
            at=at,
            reason="filled_order_surplus_released",
        )
        mutations = (*mutations, *extra)
    return mutations, final, amount, released


def _release_remaining(
    reservation: AccountReservation,
    *,
    at: datetime,
    reason: str,
) -> tuple[tuple[ReservationMutation, ...], AccountReservation, Decimal]:
    released = reservation.remaining_amount
    mutation = (
        expire_reservation(reservation, at=at)
        if at >= reservation.expires_at
        else release_reservation(reservation, at=at, reason=reason)
    )
    if isinstance(mutation, Failure):
        raise ValueError("validated order could not release reservation")
    return (mutation.value,), mutation.value.reservation, released


def _fill(
    request: PaperExecutionRequest,
    policy: PaperExecutionPolicy,
    bar: ExecutionBar,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    sequence: int,
) -> Fill:
    intent = request.command.intent
    fill_id = uuid5(
        NAMESPACE_URL,
        f"paper-fill:{request.command.command_id}:{sequence}:{bar.source_hash}:{bar.opens_at.isoformat()}",
    )
    return Fill(
        fill_id=fill_id,
        command_id=request.command.command_id,
        order_intent_id=intent.intent_id,
        account_id=intent.account_id,
        instrument_id=intent.instrument_id,
        side=intent.side,
        quantity=quantity,
        quantity_quantum=intent.quantity_quantum,
        price=price,
        price_quantum=bar.price_quantum,
        fee_currency=bar.currency,
        fees=fee,
        fee_quantum=policy.fee_quantum,
        slippage=price - bar.open,
        occurred_at=bar.opens_at,
        simulator_ref=f"{policy.execution_model_version}:{bar.source_ref}:{bar.source_hash}",
    )


def _outcome(
    request: PaperExecutionRequest,
    policy: PaperExecutionPolicy,
    events: tuple[OrderEvent, ...],
    fills: tuple[Fill, ...],
    mutations: tuple[ReservationMutation, ...],
    final: AccountReservation,
    consumed: Decimal,
    released: Decimal,
) -> Result[PaperExecutionOutcome]:
    latest = events[-1]
    receipt_id = uuid5(
        NAMESPACE_URL,
        f"paper-receipt:{request.command.command_id}:{latest.event_hash}",
    )
    receipt = ExecutionReceipt.create(
        receipt_id=receipt_id,
        command_id=request.command.command_id,
        intent=request.command.intent,
        event=latest,
        fills=fills,
        occurred_at=max(request.as_of, latest.occurred_at),
    )
    input_hash = stable_payload_hash(
        {
            "request": request.model_dump(mode="json"),
            "policy_hash": policy.policy_hash,
        }
    )
    return Success(
        PaperExecutionOutcome.create(
            receipt=receipt,
            order_events=events,
            reservation_mutations=mutations,
            final_reservation=final,
            reservation_consumed=consumed,
            reservation_released=released,
            input_hash=input_hash,
        )
    )


def _fee(quantity: Decimal, price: Decimal, policy: PaperExecutionPolicy) -> Decimal:
    raw = quantity * price * policy.fee_bps / BPS + quantity * policy.per_unit_fee
    return _ceil_quantum(max(raw, policy.minimum_fee), policy.fee_quantum)


def _cash_consumption(
    quantity: Decimal,
    price: Decimal,
    policy: PaperExecutionPolicy,
) -> Decimal:
    return quantity * price + _fee(quantity, price, policy)


def _adverse_quantize(
    value: Decimal,
    quantum: Decimal,
    side: OrderSide,
) -> Decimal:
    rounding = ROUND_CEILING if side is OrderSide.BUY else ROUND_FLOOR
    return (value / quantum).to_integral_value(rounding=rounding) * quantum


def _floor_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    return (value / quantum).to_integral_value(rounding=ROUND_FLOOR) * quantum


def _ceil_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    return (value / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum
