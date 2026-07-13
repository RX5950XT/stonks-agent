from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from stonks_agent.adapters.execution.paper import (
    ReferencePaperBroker,
    load_paper_execution_policy,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.execution_model import ExecutionBar, PaperExecutionRequest
from stonks_agent.domain.orders import OrderSide, OrderStatus, OrderType, TimeInForce
from stonks_agent.domain.reservations import ReservationState

from .helpers import ISSUED_AT, NOW, bar, command, request, reservation

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = json.loads(
    (ROOT / "tests" / "golden" / "execution" / "paper_v1.json").read_text(
        encoding="utf-8"
    )
)


def broker() -> ReferencePaperBroker:
    policy = load_paper_execution_policy(
        ROOT / "config" / "execution" / "paper_v1.yaml"
    )
    return ReferencePaperBroker(policy)


def test_market_buy_uses_first_tradable_bar_strictly_after_command() -> None:
    result = broker().execute(request())

    assert isinstance(result, Success)
    outcome = result.value
    expected = GOLDEN["market_buy_full"]
    assert outcome.receipt.status is OrderStatus.FILLED
    assert outcome.receipt.event.sequence == 2
    assert len(outcome.order_events) == 2
    assert outcome.fill is not None
    assert outcome.fill.occurred_at == ISSUED_AT + timedelta(minutes=1)
    assert str(outcome.fill.quantity) == expected["fill_quantity"]
    assert str(outcome.fill.price) == expected["fill_price"]
    assert str(outcome.fill.fees) == expected["fees"]
    assert str(outcome.fill.slippage) == expected["slippage"]
    assert str(outcome.reservation_consumed) == expected["reservation_consumed"]
    assert str(outcome.reservation_released) == expected["reservation_released"]
    assert outcome.final_reservation.state is ReservationState.RELEASED


def test_volume_participation_creates_deterministic_partial_fill() -> None:
    thin = bar(
        opens_at=ISSUED_AT + timedelta(minutes=1),
        volume="30",
    )

    result = broker().execute(request(bars=(thin,)))

    assert isinstance(result, Success)
    outcome = result.value
    expected = GOLDEN["market_buy_partial"]
    assert outcome.receipt.status is OrderStatus.PARTIALLY_FILLED
    assert outcome.fill is not None
    assert str(outcome.fill.quantity) == expected["fill_quantity"]
    assert str(outcome.fill.price) == expected["fill_price"]
    assert str(outcome.fill.fees) == expected["fees"]
    assert (
        str(outcome.final_reservation.remaining_amount)
        == expected["reservation_remaining"]
    )
    assert outcome.final_reservation.state is ReservationState.PARTIALLY_CONSUMED


def test_limit_order_intrabar_touch_fills_at_limit_not_known_close() -> None:
    limit_command = command(order_type=OrderType.LIMIT, limit_price=Decimal("100.00"))
    touched = bar(
        opens_at=ISSUED_AT + timedelta(minutes=1),
        open_price="101.00",
        high="102.00",
        low="99.00",
        close="101.50",
    )

    result = broker().execute(request(execution_command=limit_command, bars=(touched,)))

    assert isinstance(result, Success)
    assert result.value.fill is not None
    assert str(result.value.fill.price) == GOLDEN["limit_buy_touch"]["fill_price"]
    assert result.value.fill.price != touched.close


def test_sell_applies_adverse_spread_and_consumes_position_reservation() -> None:
    sell_command = command(side=OrderSide.SELL)
    held = reservation(side=OrderSide.SELL)

    result = broker().execute(request(execution_command=sell_command, held=held))

    assert isinstance(result, Success)
    outcome = result.value
    expected = GOLDEN["market_sell_full"]
    assert outcome.fill is not None
    assert str(outcome.fill.price) == expected["fill_price"]
    assert str(outcome.fill.slippage) == expected["slippage"]
    assert str(outcome.fill.fees) == expected["fees"]
    assert outcome.final_reservation.state is ReservationState.CONSUMED


def test_missing_future_bar_stays_accepted_without_manufacturing_fill() -> None:
    result = broker().execute(
        request(bars=(bar(opens_at=ISSUED_AT, source_ref="known-root"),))
    )

    assert isinstance(result, Success)
    assert result.value.receipt.status is OrderStatus.ACCEPTED
    assert result.value.fill is None
    assert result.value.reservation_mutations == ()
    assert result.value.final_reservation.state is ReservationState.OPEN


def test_uncrossed_limit_expires_and_releases_reservation() -> None:
    limit_command = command(order_type=OrderType.LIMIT, limit_price=Decimal("98.00"))
    missed = bar(
        opens_at=ISSUED_AT + timedelta(minutes=1),
        low="99.00",
    )

    result = broker().execute(
        request(
            execution_command=limit_command,
            bars=(missed,),
            as_of=NOW + timedelta(hours=1),
        )
    )

    assert isinstance(result, Success)
    assert result.value.receipt.status is OrderStatus.EXPIRED
    assert result.value.fill is None
    assert result.value.order_events[-1].occurred_at == NOW + timedelta(hours=1)
    assert result.value.final_reservation.state is ReservationState.EXPIRED


def test_ioc_partial_fill_cancels_remainder_and_releases_cash() -> None:
    ioc = command(time_in_force=TimeInForce.IOC)
    thin = bar(opens_at=ISSUED_AT + timedelta(minutes=1), volume="30")

    result = broker().execute(request(execution_command=ioc, bars=(thin,)))

    assert isinstance(result, Success)
    assert result.value.receipt.status is OrderStatus.CANCELLED
    assert tuple(event.to_status for event in result.value.order_events) == (
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCELLED,
    )
    assert result.value.final_reservation.state is ReservationState.RELEASED


def test_unsupported_stop_rejects_and_releases_without_fill() -> None:
    stopped = command(order_type=OrderType.STOP)

    result = broker().execute(request(execution_command=stopped))

    assert isinstance(result, Success)
    assert result.value.receipt.status is OrderStatus.REJECTED
    assert result.value.receipt.event.reason == "unsupported_order_type"
    assert result.value.fill is None
    assert result.value.final_reservation.state is ReservationState.RELEASED


def test_future_unavailable_or_unsorted_bars_fail_closed() -> None:
    future = bar(opens_at=ISSUED_AT + timedelta(minutes=1)).model_copy(
        update={"available_at": ISSUED_AT + timedelta(hours=2)}
    )
    later = bar(opens_at=ISSUED_AT + timedelta(minutes=2), source_ref="later")
    earlier = bar(opens_at=ISSUED_AT + timedelta(minutes=1), source_ref="earlier")

    future_result = broker().execute(request(bars=(future,)))
    unsorted_result = broker().execute(request(bars=(later, earlier)))

    assert isinstance(future_result, Failure)
    assert future_result.error.code is ErrorCode.INVALID_INPUT
    assert isinstance(unsorted_result, Failure)
    assert unsorted_result.error.code is ErrorCode.INVALID_INPUT


def test_request_rejects_reservation_or_instrument_binding_drift() -> None:
    held = reservation().model_copy(update={"event_hash": "f" * 64})
    wrong_instrument = bar(opens_at=ISSUED_AT + timedelta(minutes=1)).model_copy(
        update={"instrument_id": "45000000-0000-4000-8000-000000000099"}
    )

    try:
        PaperExecutionRequest(
            command=command(),
            reservation=held,
            prior_events=(),
            prior_fills=(),
            bars=(),
            as_of=ISSUED_AT + timedelta(minutes=2),
        )
    except ValidationError as error:
        assert "reservation" in str(error).lower()
    else:
        raise AssertionError("reservation drift should fail validation")
    result = broker().execute(request(bars=(wrong_instrument,)))
    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_policy_and_outcome_are_replay_stable() -> None:
    first = broker().execute(request())
    second = broker().execute(request())

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert second.value == first.value
    assert second.value.outcome_hash == first.value.outcome_hash
    assert len(second.value.outcome_hash) == 64


def test_execution_bar_rejects_invalid_ohlc_or_publication_timeline() -> None:
    payload = bar(opens_at=ISSUED_AT + timedelta(minutes=1)).model_dump()
    for change in (
        {"high": Decimal("98")},
        {"available_at": payload["opens_at"]},
        {"quantity_quantum": Decimal("0.3")},
    ):
        try:
            ExecutionBar.model_validate(payload | change)
        except ValidationError:
            pass
        else:
            raise AssertionError("invalid execution bar should fail validation")
