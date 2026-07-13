from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import test_trading_domain as fixtures
from pydantic import ValidationError

from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.orders import (
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    append_order_event,
    build_execution_command,
    create_order_intent,
)
from stonks_agent.domain.portfolio import CashBalance
from stonks_agent.domain.reservations import consume_reservation, create_reservation


def test_extreme_decimal_and_naive_clock_fail_closed() -> None:
    with pytest.raises(ValidationError):
        CashBalance(
            currency="USD",
            settled_amount=Decimal("1E+999999"),
            reserved_amount=Decimal("0.00"),
            quantum=Decimal("0.01"),
        )
    assert not fixtures.decision().is_current(
        account_aggregate_sequence=7,
        portfolio_sequence=3,
        at=datetime(2026, 7, 13, 10),
    )


@pytest.mark.parametrize(
    ("side", "quantity"),
    ((OrderSide.BUY, Decimal("5")), (OrderSide.SELL, Decimal("4"))),
)
def test_order_must_equal_risk_authorized_target_delta(
    side: OrderSide,
    quantity: Decimal,
) -> None:
    result = create_order_intent(
        intent_id=fixtures.INTENT_ID,
        run_id=fixtures.UUID("41000000-0000-4000-8000-000000000010"),
        decision=fixtures.decision(),
        reservation=fixtures.reservation(),
        instrument_id=fixtures.INSTRUMENT_ID,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        quantity_quantum=Decimal("1"),
        limit_price=Decimal("100.00"),
        stop_price=None,
        time_in_force=TimeInForce.DAY,
        valid_from=fixtures.NOW + timedelta(minutes=2),
        valid_until=fixtures.NOW + timedelta(minutes=5),
        idempotency_key="paper-account:run:order-1",
        execution_model_version="paper-v1",
        created_at=fixtures.NOW + timedelta(minutes=2),
    )

    assert isinstance(result, Failure)


def test_non_expiry_order_event_cannot_land_at_valid_until() -> None:
    accepted = append_order_event(
        fixtures.intent(),
        previous=None,
        target_status=OrderStatus.ACCEPTED,
        cumulative_filled_quantity=Decimal("0"),
        occurred_at=fixtures.NOW + timedelta(minutes=3),
    )
    assert isinstance(accepted, Success)
    late_fill = append_order_event(
        fixtures.intent(),
        previous=accepted.value,
        target_status=OrderStatus.FILLED,
        cumulative_filled_quantity=Decimal("4"),
        occurred_at=fixtures.NOW + timedelta(minutes=5),
    )
    expired = append_order_event(
        fixtures.intent(),
        previous=accepted.value,
        target_status=OrderStatus.EXPIRED,
        cumulative_filled_quantity=Decimal("0"),
        occurred_at=fixtures.NOW + timedelta(minutes=5),
        reason="validity_elapsed",
    )

    assert isinstance(late_fill, Failure)
    assert isinstance(expired, Success)


def test_naive_reservation_and_command_clocks_return_structured_failure() -> None:
    naive = datetime(2026, 7, 13, 10)
    invalid_expiry = create_reservation(
        reservation_id=fixtures.RESERVATION_ID,
        order_intent_id=fixtures.INTENT_ID,
        decision=fixtures.decision(),
        kind=fixtures.ReservationKind.CASH,
        commodity="USD",
        amount=Decimal("405.00"),
        quantum=Decimal("0.01"),
        instrument_id=fixtures.INSTRUMENT_ID,
        at=fixtures.NOW + timedelta(minutes=2),
        expires_at=naive,
        current_account_sequence=7,
        current_portfolio_sequence=3,
    )
    invalid_consume = consume_reservation(
        fixtures.reservation(), amount=Decimal("1.00"), at=naive
    )
    invalid_command = build_execution_command(
        command_id=fixtures.COMMAND_ID,
        intent=fixtures.intent(),
        decision=fixtures.decision(),
        reservation=fixtures.reservation(),
        current_account_sequence=8,
        current_portfolio_sequence=3,
        attempt_generation=1,
        attempt_nonce="nonce-1",
        issued_at=naive,
    )

    assert isinstance(invalid_expiry, Failure)
    assert isinstance(invalid_consume, Failure)
    assert isinstance(invalid_command, Failure)
