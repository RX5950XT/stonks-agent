from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.domain.errors import Success
from stonks_agent.domain.execution_model import ExecutionBar, PaperExecutionRequest
from stonks_agent.domain.orders import (
    ExecutionCommand,
    OrderSide,
    OrderType,
    TimeInForce,
    build_execution_command,
    create_order_intent,
)
from stonks_agent.domain.portfolio import PortfolioTarget, TargetAllocation
from stonks_agent.domain.reservations import (
    AccountReservation,
    ReservationKind,
    create_reservation,
)
from stonks_agent.domain.risk import RiskCheck, RiskDecision

NOW = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
ISSUED_AT = NOW + timedelta(minutes=1)
ACCOUNT_ID = "paper-execution"
INSTRUMENT_ID = UUID("45000000-0000-4000-8000-000000000001")
TARGET_ID = UUID("45000000-0000-4000-8000-000000000002")
SNAPSHOT_ID = UUID("45000000-0000-4000-8000-000000000003")
DECISION_ID = UUID("45000000-0000-4000-8000-000000000004")
RESERVATION_ID = UUID("45000000-0000-4000-8000-000000000005")
INTENT_ID = UUID("45000000-0000-4000-8000-000000000006")
COMMAND_ID = UUID("45000000-0000-4000-8000-000000000007")
HASH_A = "a" * 64
HASH_B = "b" * 64


def target(*, side: OrderSide = OrderSide.BUY) -> PortfolioTarget:
    current = Decimal("0") if side is OrderSide.BUY else Decimal("5")
    wanted = Decimal("5") if side is OrderSide.BUY else Decimal("0")
    return PortfolioTarget.create(
        target_id=TARGET_ID,
        account_id=ACCOUNT_ID,
        portfolio_snapshot_id=SNAPSHOT_ID,
        account_aggregate_sequence=7,
        portfolio_sequence=3,
        as_of=NOW,
        allocations=(
            TargetAllocation(
                instrument_id=INSTRUMENT_ID,
                current_quantity=current,
                target_quantity=wanted,
                delta_quantity=wanted - current,
                quantity_quantum=Decimal("1"),
                target_weight=Decimal("0.05") if wanted else Decimal("0"),
            ),
        ),
        input_signal_ids=(UUID("45000000-0000-4000-8000-000000000008"),),
        policy_version="1.0.0",
        policy_hash=HASH_A,
        expected_turnover=Decimal("0.05"),
        expected_cost=Decimal("1.00"),
        cost_currency="USD",
    )


def decision(*, side: OrderSide = OrderSide.BUY) -> RiskDecision:
    authorized = target(side=side)
    return RiskDecision.create(
        decision_id=DECISION_ID,
        target=authorized,
        approved=True,
        normalized_target=authorized,
        checks=(RiskCheck(code="paper_execution", passed=True),),
        policy_version="1.0.0",
        policy_hash=HASH_B,
        decided_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def reservation(*, side: OrderSide = OrderSide.BUY) -> AccountReservation:
    created = create_reservation(
        reservation_id=RESERVATION_ID,
        order_intent_id=INTENT_ID,
        decision=decision(side=side),
        kind=(
            ReservationKind.CASH if side is OrderSide.BUY else ReservationKind.POSITION
        ),
        commodity="USD" if side is OrderSide.BUY else str(INSTRUMENT_ID),
        amount=Decimal("1000.00") if side is OrderSide.BUY else Decimal("5"),
        quantum=Decimal("0.01") if side is OrderSide.BUY else Decimal("1"),
        instrument_id=INSTRUMENT_ID,
        at=ISSUED_AT,
        expires_at=NOW + timedelta(hours=1),
        current_account_sequence=7,
        current_portfolio_sequence=3,
    )
    assert isinstance(created, Success)
    return created.value.reservation


def command(
    *,
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    time_in_force: TimeInForce = TimeInForce.DAY,
    limit_price: Decimal | None = None,
) -> ExecutionCommand:
    held = reservation(side=side)
    created = create_order_intent(
        intent_id=INTENT_ID,
        run_id=UUID("45000000-0000-4000-8000-000000000009"),
        decision=decision(side=side),
        reservation=held,
        instrument_id=INSTRUMENT_ID,
        side=side,
        order_type=order_type,
        quantity=Decimal("5"),
        quantity_quantum=Decimal("1"),
        limit_price=limit_price,
        stop_price=Decimal("99.00") if order_type is OrderType.STOP else None,
        time_in_force=time_in_force,
        valid_from=ISSUED_AT,
        valid_until=NOW + timedelta(hours=1),
        idempotency_key="paper-execution:order-1",
        execution_model_version="paper-v1",
        created_at=ISSUED_AT,
    )
    assert isinstance(created, Success)
    built = build_execution_command(
        command_id=COMMAND_ID,
        intent=created.value,
        decision=decision(side=side),
        reservation=held,
        current_account_sequence=8,
        current_portfolio_sequence=3,
        attempt_generation=1,
        attempt_nonce="paper-attempt-1",
        issued_at=ISSUED_AT,
    )
    assert isinstance(built, Success)
    return built.value


def bar(
    *,
    opens_at: datetime,
    open_price: str = "100.00",
    high: str = "101.00",
    low: str = "99.00",
    close: str = "100.50",
    volume: str = "100",
    source_ref: str = "bar-next",
    tradable: bool = True,
) -> ExecutionBar:
    closes_at = opens_at + timedelta(seconds=30)
    return ExecutionBar(
        instrument_id=INSTRUMENT_ID,
        mic="XNAS",
        opens_at=opens_at,
        closes_at=closes_at,
        available_at=closes_at,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        currency="USD",
        price_quantum=Decimal("0.01"),
        quantity_quantum=Decimal("1"),
        source_ref=source_ref,
        source_hash=HASH_A,
        tradable=tradable,
    )


def request(
    *,
    execution_command: ExecutionCommand | None = None,
    held: AccountReservation | None = None,
    bars: tuple[ExecutionBar, ...] | None = None,
    as_of: datetime | None = None,
) -> PaperExecutionRequest:
    actual_command = execution_command or command()
    actual_reservation = held or reservation(side=actual_command.intent.side)
    actual_bars = bars or (
        bar(opens_at=ISSUED_AT, source_ref="known-root"),
        bar(opens_at=ISSUED_AT + timedelta(minutes=1)),
    )
    return PaperExecutionRequest(
        command=actual_command,
        reservation=actual_reservation,
        prior_events=(),
        prior_fills=(),
        bars=actual_bars,
        as_of=as_of or ISSUED_AT + timedelta(minutes=2),
    )
