"""Recheck risk and atomically reserve balances while creating paper orders."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from stonks_agent.application.risk.evaluate import evaluate
from stonks_agent.domain._trading import failure
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.orders import (
    OrderIntent,
    OrderSide,
    OrderType,
    TimeInForce,
    create_order_intent,
)
from stonks_agent.domain.portfolio import PaperAccountState, TargetAllocation
from stonks_agent.domain.reservations import (
    ReservationKind,
    ReservationMutation,
    create_reservation,
)
from stonks_agent.domain.risk import RiskDecision
from stonks_agent.domain.risk_authorization import (
    PlannedPaperOrder,
    RiskAuthorizationCommand,
    RiskAuthorizationResult,
)
from stonks_agent.domain.risk_evaluation import HardRiskPolicy
from stonks_agent.ports.trading_unit_of_work import TradingUnitOfWorkFactory

ZERO = Decimal(0)


def evaluate_and_authorize(
    command: RiskAuthorizationCommand,
    policy: HardRiskPolicy,
    unit_of_work: TradingUnitOfWorkFactory,
) -> Result[RiskAuthorizationResult]:
    with unit_of_work() as transaction:
        account = transaction.trading.get_account(command.risk.snapshot.account_id)
        if isinstance(account, Failure):
            return account
        if not _account_matches(account.value, command):
            return failure(
                ErrorCode.CONFLICT,
                "Paper account changed before risk authorization",
            )
        evaluated = evaluate(command.risk, policy)
        if isinstance(evaluated, Failure):
            return evaluated
        saved_target = transaction.trading.save_target(command.risk.target)
        if isinstance(saved_target, Failure):
            return saved_target
        saved_decision = transaction.trading.save_risk_decision(evaluated.value)
        if isinstance(saved_decision, Failure):
            return saved_decision
        if not evaluated.value.approved:
            transaction.commit()
            return Success(RiskAuthorizationResult(decision=evaluated.value))
        pairs = _build_pairs(command, evaluated.value)
        if isinstance(pairs, Failure):
            return pairs
        if not pairs.value:
            transaction.commit()
            return Success(RiskAuthorizationResult(decision=evaluated.value))
        created = transaction.trading.create_reservation_orders(pairs.value)
        if isinstance(created, Failure):
            return created
        transaction.commit()
        return Success(
            RiskAuthorizationResult(
                decision=evaluated.value,
                orders=created.value,
            )
        )


def _account_matches(
    account: PaperAccountState,
    command: RiskAuthorizationCommand,
) -> bool:
    snapshot = command.risk.snapshot
    return (
        account.account_id == snapshot.account_id
        and account.base_currency == command.risk.target.cost_currency
        and account.account_aggregate_sequence == snapshot.account_aggregate_sequence
        and account.portfolio_sequence == snapshot.portfolio_sequence
        and account.ledger_sequence == snapshot.ledger_sequence
        and account.ledger_hash == snapshot.ledger_hash
        and account.cash == snapshot.cash
        and account.positions == snapshot.positions
        and account.updated_at <= command.risk.at
    )


def _build_pairs(
    command: RiskAuthorizationCommand,
    decision: RiskDecision,
) -> Result[tuple[tuple[ReservationMutation, OrderIntent], ...]]:
    target = decision.normalized_target
    if target is None:
        return failure(ErrorCode.CONFLICT, "Approved risk decision has no target")
    allocations = tuple(item for item in target.allocations if item.delta_quantity != 0)
    expected = {item.instrument_id for item in allocations}
    plans = {item.instrument_id: item for item in command.orders}
    if set(plans) != expected or len(plans) != len(command.orders):
        return failure(
            ErrorCode.CONFLICT,
            "Planned orders do not exactly cover authorized target deltas",
        )
    unsupported = next(
        (item for item in command.orders if not _supported_order(item)),
        None,
    )
    if unsupported is not None:
        return failure(
            ErrorCode.CAPABILITY_DENIED,
            "Only market day orders are allowed by the paper risk gate",
            instrument_id=str(unsupported.instrument_id),
        )
    instruments = {item.instrument_id: item for item in command.risk.instruments}
    cash = next(
        (
            item
            for item in command.risk.snapshot.cash
            if item.currency == target.cost_currency
        ),
        None,
    )
    if cash is None or any(
        item.instrument_id not in instruments for item in allocations
    ):
        return failure(
            ErrorCode.DATA_UNAVAILABLE, "Authorization pricing is unavailable"
        )
    notionals = {
        item.instrument_id: abs(item.delta_quantity)
        * instruments[item.instrument_id].mark_price
        for item in allocations
    }
    total_notional = sum(notionals.values(), ZERO)
    cost_shares = {
        item.instrument_id: (
            target.expected_cost * notionals[item.instrument_id] / total_notional
            if item.delta_quantity > 0 and total_notional > 0
            else ZERO
        )
        for item in allocations
    }
    built: list[tuple[ReservationMutation, OrderIntent]] = []
    for allocation in allocations:
        pair = _build_pair(
            allocation,
            plans[allocation.instrument_id],
            decision,
            command,
            instruments[allocation.instrument_id].mark_price,
            cash.quantum,
            cost_share=cost_shares[allocation.instrument_id],
        )
        if isinstance(pair, Failure):
            return pair
        built.append(pair.value)
    return Success(tuple(built))


def _build_pair(
    allocation: TargetAllocation,
    plan: PlannedPaperOrder,
    decision: RiskDecision,
    command: RiskAuthorizationCommand,
    mark_price: Decimal,
    cash_quantum: Decimal,
    *,
    cost_share: Decimal,
) -> Result[tuple[ReservationMutation, OrderIntent]]:
    buying = allocation.delta_quantity > 0
    side = OrderSide.BUY if buying else OrderSide.SELL
    kind = ReservationKind.CASH if buying else ReservationKind.POSITION
    amount = abs(allocation.delta_quantity)
    quantum = allocation.quantity_quantum
    commodity = str(allocation.instrument_id)
    if buying:
        amount = amount * mark_price + cost_share
        amount = _ceil_to_quantum(amount, cash_quantum)
        quantum = cash_quantum
        commodity = command.risk.target.cost_currency
    reserved = create_reservation(
        reservation_id=plan.reservation_id,
        order_intent_id=plan.intent_id,
        decision=decision,
        kind=kind,
        commodity=commodity,
        amount=amount,
        quantum=quantum,
        instrument_id=allocation.instrument_id,
        at=command.risk.at,
        expires_at=decision.expires_at,
        current_account_sequence=command.risk.snapshot.account_aggregate_sequence,
        current_portfolio_sequence=command.risk.snapshot.portfolio_sequence,
    )
    if isinstance(reserved, Failure):
        return reserved
    intent = create_order_intent(
        intent_id=plan.intent_id,
        run_id=plan.run_id,
        decision=decision,
        reservation=reserved.value.reservation,
        instrument_id=allocation.instrument_id,
        side=side,
        order_type=plan.order_type,
        quantity=abs(allocation.delta_quantity),
        quantity_quantum=allocation.quantity_quantum,
        limit_price=plan.limit_price,
        stop_price=plan.stop_price,
        time_in_force=plan.time_in_force,
        valid_from=command.risk.at,
        valid_until=decision.expires_at,
        idempotency_key=plan.idempotency_key,
        execution_model_version=plan.execution_model_version,
        created_at=command.risk.at,
    )
    if isinstance(intent, Failure):
        return intent
    return Success((reserved.value, intent.value))


def _supported_order(plan: PlannedPaperOrder) -> bool:
    return (
        plan.order_type is OrderType.MARKET
        and plan.time_in_force is TimeInForce.DAY
        and plan.limit_price is None
        and plan.stop_price is None
    )


def _ceil_to_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    units = (value / quantum).to_integral_value(rounding=ROUND_CEILING)
    return units * quantum
