"""Cross-aggregate immutable results returned by trading persistence ports."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel
from stonks_agent.domain.orders import OrderIntent
from stonks_agent.domain.portfolio import PaperAccountEvent
from stonks_agent.domain.reservations import AccountReservation, ReservationEvent


class ReservationOrderRecord(TradingModel):
    reservation: AccountReservation
    reservation_event: ReservationEvent
    order_intent: OrderIntent
    account_event: PaperAccountEvent

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if (
            self.reservation.reservation_id != self.reservation_event.reservation_id
            or self.reservation.order_intent_id != self.order_intent.intent_id
            or self.reservation.reservation_id != self.order_intent.reservation_id
            or self.reservation.account_id != self.order_intent.account_id
            or self.reservation.event_hash != self.order_intent.reservation_hash
            or self.account_event.account_id != self.order_intent.account_id
            or self.account_event.sequence
            != self.order_intent.account_aggregate_sequence
            or self.account_event.aggregate_ref_type != "reservation_order"
            or self.account_event.aggregate_ref_id != self.order_intent.intent_id
        ):
            raise ValueError("reservation/order persistence bindings are invalid")
        return self


class ReservationOrderItem(TradingModel):
    reservation: AccountReservation
    reservation_event: ReservationEvent
    order_intent: OrderIntent

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if (
            self.reservation.reservation_id != self.reservation_event.reservation_id
            or self.reservation.order_intent_id != self.order_intent.intent_id
            or self.reservation.reservation_id != self.order_intent.reservation_id
            or self.reservation.account_id != self.order_intent.account_id
            or self.reservation.event_hash != self.order_intent.reservation_hash
        ):
            raise ValueError("reservation/order item bindings are invalid")
        return self


class ReservationOrderBatchRecord(TradingModel):
    items: tuple[ReservationOrderItem, ...] = Field(min_length=1, max_length=100_000)
    account_event: PaperAccountEvent

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        intents = tuple(item.order_intent for item in self.items)
        if intents != tuple(sorted(intents, key=lambda item: str(item.intent_id))):
            raise ValueError("reservation/order batch must be stably sorted")
        account_ids = {item.account_id for item in intents}
        sequences = {item.account_aggregate_sequence for item in intents}
        decisions = {item.risk_decision_id for item in intents}
        if (
            len(account_ids) != 1
            or len(sequences) != 1
            or len(decisions) != 1
            or self.account_event.account_id not in account_ids
            or self.account_event.sequence not in sequences
            or self.account_event.aggregate_ref_type != "reservation_orders"
            or self.account_event.aggregate_ref_id not in decisions
        ):
            raise ValueError("reservation/order batch bindings are invalid")
        return self
