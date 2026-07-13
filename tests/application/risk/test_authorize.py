from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import TracebackType
from uuid import UUID

from stonks_agent.application.risk.authorize import (
    PlannedPaperOrder,
    RiskAuthorizationCommand,
    evaluate_and_authorize,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.orders import OrderIntent, OrderType, TimeInForce
from stonks_agent.domain.portfolio import (
    PaperAccountEvent,
    PaperAccountState,
    PortfolioTarget,
    TargetAllocation,
)
from stonks_agent.domain.reservations import ReservationMutation
from stonks_agent.domain.risk import RiskDecision
from stonks_agent.domain.trading_persistence import (
    ReservationOrderBatchRecord,
    ReservationOrderItem,
)

from .helpers import RISK_AT, configured_policy, risk_command


class FakeTradingRepository:
    def __init__(
        self,
        state: PaperAccountState,
        *,
        fail_batch: bool = False,
    ) -> None:
        self.state = state
        self.fail_batch = fail_batch
        self.saved_targets: list[PortfolioTarget] = []
        self.saved_decisions: list[RiskDecision] = []
        self.pairs: tuple[tuple[ReservationMutation, OrderIntent], ...] = ()

    def get_account(self, account_id: str) -> Result[PaperAccountState]:
        if account_id != self.state.account_id:
            return Failure(
                StructuredError(code=ErrorCode.NOT_FOUND, message="missing account")
            )
        return Success(self.state)

    def save_target(self, target: PortfolioTarget) -> Result[PortfolioTarget]:
        self.saved_targets.append(target)
        return Success(target)

    def save_risk_decision(self, decision: RiskDecision) -> Result[RiskDecision]:
        self.saved_decisions.append(decision)
        return Success(decision)

    def create_reservation_orders(
        self,
        pairs: tuple[tuple[ReservationMutation, OrderIntent], ...],
    ) -> Result[ReservationOrderBatchRecord]:
        self.pairs = pairs
        if self.fail_batch:
            return Failure(
                StructuredError(
                    code=ErrorCode.CONFLICT,
                    message="simulated batch conflict",
                )
            )
        decision = self.saved_decisions[-1]
        event_values = {
            "event_id": UUID("44000000-0000-4000-8000-000000000090"),
            "account_id": decision.account_id,
            "sequence": decision.account_aggregate_sequence + 1,
            "event_type": "reservation_orders.created",
            "aggregate_ref_type": "reservation_orders",
            "aggregate_ref_id": decision.decision_id,
            "occurred_at": RISK_AT,
            "previous_hash": "a" * 64,
        }
        provisional = PaperAccountEvent.model_construct(
            **event_values,
            event_hash="0" * 64,
        )
        event = PaperAccountEvent(
            **event_values,
            event_hash=provisional.expected_event_hash(),
        )
        return Success(
            ReservationOrderBatchRecord(
                items=tuple(
                    ReservationOrderItem(
                        reservation=mutation.reservation,
                        reservation_event=mutation.event,
                        order_intent=intent,
                    )
                    for mutation, intent in pairs
                ),
                account_event=event,
            )
        )


class FakeUnitOfWork:
    def __init__(self, repository: FakeTradingRepository) -> None:
        self.trading = repository
        self.committed = False

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.committed = False


def account_state(*, sequence: int = 7) -> PaperAccountState:
    snapshot = risk_command().snapshot
    return PaperAccountState.model_construct(
        account_id=snapshot.account_id,
        base_currency="USD",
        account_aggregate_sequence=sequence,
        portfolio_sequence=snapshot.portfolio_sequence,
        ledger_sequence=snapshot.ledger_sequence,
        ledger_hash=snapshot.ledger_hash,
        cash=snapshot.cash,
        positions=snapshot.positions,
        events=(),
        created_at=snapshot.as_of - timedelta(days=1),
        updated_at=snapshot.as_of,
    )


def plan(*, order_type: OrderType = OrderType.MARKET) -> PlannedPaperOrder:
    return PlannedPaperOrder(
        instrument_id=risk_command().target.allocations[0].instrument_id,
        reservation_id=UUID("44000000-0000-4000-8000-000000000091"),
        intent_id=UUID("44000000-0000-4000-8000-000000000092"),
        run_id=UUID("44000000-0000-4000-8000-000000000093"),
        idempotency_key="risk-authorization:order-1",
        order_type=order_type,
        time_in_force=TimeInForce.DAY,
        limit_price=Decimal("100") if order_type is OrderType.LIMIT else None,
        stop_price=None,
        execution_model_version="paper-v1",
    )


def authorize_command(**changes: object) -> RiskAuthorizationCommand:
    values: dict[str, object] = {
        "risk": risk_command(),
        "orders": (plan(),),
    }
    return RiskAuthorizationCommand.model_validate(values | changes)


def sell_risk_command() -> object:
    base = risk_command()
    allocation = TargetAllocation(
        instrument_id=base.target.allocations[0].instrument_id,
        current_quantity=Decimal("10"),
        target_quantity=Decimal("0"),
        delta_quantity=Decimal("-10"),
        quantity_quantum=Decimal("1"),
        target_weight=Decimal("0"),
    )
    sell_target = PortfolioTarget.create(
        target_id=base.target.target_id,
        account_id=base.target.account_id,
        portfolio_snapshot_id=base.target.portfolio_snapshot_id,
        account_aggregate_sequence=base.target.account_aggregate_sequence,
        portfolio_sequence=base.target.portfolio_sequence,
        as_of=base.target.as_of,
        allocations=(allocation,),
        input_signal_ids=base.target.input_signal_ids,
        policy_version=base.target.policy_version,
        policy_hash=base.target.policy_hash,
        expected_turnover=Decimal("0.090909090909"),
        expected_cost=Decimal("0.80"),
        cost_currency=base.target.cost_currency,
    )
    return base.model_copy(update={"target": sell_target})


def test_approved_risk_is_rechecked_reserved_and_ordered_in_one_commit() -> None:
    repository = FakeTradingRepository(account_state())
    unit_of_work = FakeUnitOfWork(repository)

    result = evaluate_and_authorize(
        authorize_command(),
        configured_policy(),
        lambda: unit_of_work,
    )

    assert isinstance(result, Success)
    assert result.value.decision.approved is True
    assert result.value.orders is not None
    assert unit_of_work.committed is True
    assert len(repository.pairs) == 1
    reservation, intent = repository.pairs[0]
    assert reservation.reservation.amount == Decimal("900.72")
    assert reservation.reservation.account_aggregate_sequence == 8
    assert intent.quantity == Decimal("9")
    assert intent.side.value == "buy"


def test_rejected_risk_is_audited_without_creating_orders() -> None:
    repository = FakeTradingRepository(account_state())
    unit_of_work = FakeUnitOfWork(repository)
    killed = risk_command(
        kill_switch={
            "global_active": True,
            "account_active": False,
            "observed_at": RISK_AT,
        }
    )

    result = evaluate_and_authorize(
        authorize_command(risk=killed),
        configured_policy(),
        lambda: unit_of_work,
    )

    assert isinstance(result, Success)
    assert result.value.decision.approved is False
    assert result.value.orders is None
    assert repository.pairs == ()
    assert len(repository.saved_decisions) == 1
    assert unit_of_work.committed is True


def test_account_sequence_drift_and_unsupported_order_fail_without_commit() -> None:
    stale_repository = FakeTradingRepository(account_state(sequence=8))
    stale_uow = FakeUnitOfWork(stale_repository)
    stale = evaluate_and_authorize(
        authorize_command(),
        configured_policy(),
        lambda: stale_uow,
    )
    order_repository = FakeTradingRepository(account_state())
    order_uow = FakeUnitOfWork(order_repository)
    unsupported = evaluate_and_authorize(
        authorize_command(orders=(plan(order_type=OrderType.LIMIT),)),
        configured_policy(),
        lambda: order_uow,
    )

    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT
    assert stale_uow.committed is False
    assert isinstance(unsupported, Failure)
    assert unsupported.error.code is ErrorCode.CAPABILITY_DENIED
    assert order_uow.committed is False


def test_sell_authorization_reserves_exact_available_position() -> None:
    repository = FakeTradingRepository(account_state())
    unit_of_work = FakeUnitOfWork(repository)

    result = evaluate_and_authorize(
        authorize_command(risk=sell_risk_command()),
        configured_policy(),
        lambda: unit_of_work,
    )

    assert isinstance(result, Success)
    reservation, intent = repository.pairs[0]
    assert reservation.reservation.kind.value == "position"
    assert reservation.reservation.amount == Decimal("10")
    assert intent.side.value == "sell"


def test_missing_plan_and_repository_conflict_leave_transaction_uncommitted() -> None:
    missing_repository = FakeTradingRepository(account_state())
    missing_uow = FakeUnitOfWork(missing_repository)
    missing = evaluate_and_authorize(
        authorize_command(orders=()),
        configured_policy(),
        lambda: missing_uow,
    )
    conflict_repository = FakeTradingRepository(account_state(), fail_batch=True)
    conflict_uow = FakeUnitOfWork(conflict_repository)
    conflict = evaluate_and_authorize(
        authorize_command(),
        configured_policy(),
        lambda: conflict_uow,
    )

    assert isinstance(missing, Failure)
    assert missing.error.code is ErrorCode.CONFLICT
    assert missing_uow.committed is False
    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT
    assert conflict_uow.committed is False
