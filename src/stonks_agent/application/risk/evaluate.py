"""Deterministic hard-risk evaluation over a complete point-in-time state."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from uuid import UUID

import yaml

from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.portfolio import CashBalance, PositionBalance
from stonks_agent.domain.reservations import ReservationKind, ReservationState
from stonks_agent.domain.risk import RiskCheck, RiskDecision
from stonks_agent.domain.risk_evaluation import (
    BuildRiskDecisionCommand,
    HardRiskPolicy,
    RiskInstrumentState,
)
from stonks_agent.domain.signal import evaluate_signal_eligibility

CALCULATION_QUANTUM = Decimal("0.000000000001")
ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class _EvaluationState:
    command: BuildRiskDecisionCommand
    instruments: dict[UUID, RiskInstrumentState]
    duplicate_instruments: bool
    positions: dict[UUID, PositionBalance]
    cash: CashBalance | None
    nav: Decimal | None


@dataclass(frozen=True, slots=True)
class HardRiskEvaluator:
    policy: HardRiskPolicy

    def evaluate(self, command: BuildRiskDecisionCommand) -> Result[RiskDecision]:
        return evaluate(command, self.policy)


def load_risk_policy(path: str | Path) -> HardRiskPolicy:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return HardRiskPolicy.model_validate(payload)
    except (OSError, yaml.YAMLError, TypeError) as error:
        raise ValueError("hard risk policy could not be loaded") from error


def evaluate(
    command: BuildRiskDecisionCommand,
    policy: HardRiskPolicy,
) -> Result[RiskDecision]:
    state = _evaluation_state(command)
    checks = tuple(
        sorted(
            (
                _adv_check(state, policy),
                _asset_class_check(state, policy),
                _cash_check(state),
                _daily_loss_check(state, policy),
                _data_check(state, policy),
                _drawdown_check(state, policy),
                _gross_check(state, policy),
                _kill_switch_check(command, policy),
                _ledger_check(command),
                _market_session_check(state, policy),
                _net_check(state, policy),
                _pending_orders_check(command, policy),
                _position_check(state),
                _reservation_check(command),
                _sector_check(state, policy),
                _signal_check(command),
                _single_position_check(command, policy),
                _target_binding_check(command),
                _turnover_check(command, policy),
            ),
            key=lambda item: item.code,
        )
    )
    approved = all(item.passed for item in checks)
    return Success(
        RiskDecision.create(
            decision_id=command.decision_id,
            target=command.target,
            approved=approved,
            normalized_target=command.target if approved else None,
            checks=checks,
            policy_version=policy.policy_version,
            policy_hash=policy.policy_hash,
            decided_at=command.at,
            expires_at=command.at + timedelta(seconds=policy.decision_valid_seconds),
        )
    )


def _evaluation_state(command: BuildRiskDecisionCommand) -> _EvaluationState:
    instruments = {item.instrument_id: item for item in command.instruments}
    positions = {item.instrument_id: item for item in command.snapshot.positions}
    cash = next(
        (
            item
            for item in command.snapshot.cash
            if item.currency == command.target.cost_currency
        ),
        None,
    )
    missing_marks = any(
        item.instrument_id not in instruments for item in positions.values()
    )
    nav = None
    if cash is not None and not missing_marks:
        nav = cash.settled_amount + sum(
            (
                item.quantity * instruments[item.instrument_id].mark_price
                for item in positions.values()
            ),
            ZERO,
        )
    return _EvaluationState(
        command=command,
        instruments=instruments,
        duplicate_instruments=len(instruments) != len(command.instruments),
        positions=positions,
        cash=cash,
        nav=nav if nav is not None and nav > 0 else None,
    )


def _target_binding_check(command: BuildRiskDecisionCommand) -> RiskCheck:
    snapshot = command.snapshot
    target = command.target
    allocation_quantities = {
        item.instrument_id: item.current_quantity for item in target.allocations
    }
    position_quantities = {
        item.instrument_id: item.quantity for item in snapshot.positions
    }
    position_matches = all(
        allocation_quantities.get(instrument_id) == quantity
        for instrument_id, quantity in position_quantities.items()
    ) and all(
        instrument_id in position_quantities or quantity == 0
        for instrument_id, quantity in allocation_quantities.items()
    )
    matches = (
        target.calculation_hash == target.expected_calculation_hash()
        and target.account_id == snapshot.account_id
        and target.portfolio_snapshot_id == snapshot.snapshot_id
        and target.account_aggregate_sequence == snapshot.account_aggregate_sequence
        and target.portfolio_sequence == snapshot.portfolio_sequence
        and target.as_of == snapshot.as_of
        and target.as_of <= command.at
        and position_matches
    )
    return _check(
        "target_binding",
        matches,
        reason="target does not match current account snapshot",
    )


def _data_check(state: _EvaluationState, policy: HardRiskPolicy) -> RiskCheck:
    command = state.command
    required = {
        *(item.instrument_id for item in command.target.allocations),
        *(item.instrument_id for item in command.snapshot.positions),
    }
    invalid = state.duplicate_instruments or any(
        instrument_id not in state.instruments for instrument_id in required
    )
    ages: list[Decimal] = []
    allocations = {item.instrument_id: item for item in command.target.allocations}
    for instrument_id in required & set(state.instruments):
        instrument = state.instruments[instrument_id]
        age = Decimal(str((command.at - instrument.mark_as_of).total_seconds()))
        ages.append(age)
        allocation = allocations.get(instrument_id)
        invalid = invalid or age < 0 or age > policy.max_data_age_seconds
        invalid = invalid or instrument.currency != command.target.cost_currency
        if allocation is not None:
            invalid = (
                invalid or instrument.quantity_quantum != allocation.quantity_quantum
            )
    actual = max(ages, default=ZERO)
    return _check(
        "data_freshness",
        not invalid,
        actual=actual,
        limit=Decimal(policy.max_data_age_seconds),
        reason="mark data is missing, stale, future, or contract-incompatible",
    )


def _signal_check(command: BuildRiskDecisionCommand) -> RiskCheck:
    candidates = command.signal_candidates
    ids = tuple(item.signal.signal_id for item in candidates)
    keys = tuple(
        (
            item.signal.instrument_id,
            item.signal.strategy_id,
            item.signal.strategy_version,
        )
        for item in candidates
    )
    invalid = len(ids) != len(set(ids)) or len(keys) != len(set(keys))
    invalid = invalid or set(ids) != set(command.target.input_signal_ids)
    candidate_instruments: set[UUID] = set()
    for candidate in candidates:
        signal = candidate.signal
        candidate_instruments.add(signal.instrument_id)
        decision = evaluate_signal_eligibility(
            signal,
            registry=candidate.registry,
            evaluation=candidate.evaluation,
            at=command.at,
        )
        invalid = invalid or not decision.eligible
        invalid = invalid or signal.generated_at > command.target.as_of
        invalid = invalid or candidate.registry.updated_at > command.at
        invalid = invalid or candidate.evaluation.created_at > command.at
    increased = {
        item.instrument_id
        for item in command.target.allocations
        if item.delta_quantity > 0
    }
    invalid = invalid or not increased <= candidate_instruments
    return _check(
        "signal_freshness",
        not invalid,
        actual=Decimal(
            sum(
                item.signal.signal_id not in command.target.input_signal_ids
                for item in candidates
            )
        ),
        limit=ZERO,
        reason="signal set is missing, stale, ineligible, future, or binding-incompatible",
    )


def _ledger_check(command: BuildRiskDecisionCommand) -> RiskCheck:
    head = command.ledger_head
    snapshot = command.snapshot
    matches = (
        head.account_id == snapshot.account_id
        and head.sequence == snapshot.ledger_sequence
        and head.transaction_hash == snapshot.ledger_hash
    )
    return _check(
        "ledger_binding",
        matches,
        actual=Decimal(head.sequence),
        limit=Decimal(snapshot.ledger_sequence),
        reason="ledger head does not match account snapshot",
    )


def _kill_switch_check(
    command: BuildRiskDecisionCommand,
    policy: HardRiskPolicy,
) -> RiskCheck:
    switch = command.kill_switch
    age = Decimal(str((command.at - switch.observed_at).total_seconds()))
    passed = (
        age >= 0
        and age <= policy.max_kill_switch_age_seconds
        and not switch.global_active
        and not switch.account_active
    )
    return _check(
        "kill_switch",
        passed,
        actual=age,
        limit=Decimal(policy.max_kill_switch_age_seconds),
        reason="kill switch is active, stale, or from the future",
    )


def _reservation_check(command: BuildRiskDecisionCommand) -> RiskCheck:
    cash_reserved: defaultdict[str, Decimal] = defaultdict(Decimal)
    position_reserved: defaultdict[UUID, Decimal] = defaultdict(Decimal)
    order_ids: set[UUID] = set()
    valid = True
    for reservation in command.open_reservations:
        valid = valid and reservation.state in {
            ReservationState.OPEN,
            ReservationState.PARTIALLY_CONSUMED,
        }
        valid = valid and reservation.account_id == command.snapshot.account_id
        valid = valid and reservation.updated_at <= command.at < reservation.expires_at
        order_ids.add(reservation.order_intent_id)
        if reservation.kind is ReservationKind.CASH:
            cash_reserved[reservation.commodity] += reservation.remaining_amount
        elif reservation.instrument_id is not None:
            position_reserved[reservation.instrument_id] += reservation.remaining_amount
    expected_cash = {
        item.currency: item.reserved_amount for item in command.snapshot.cash
    }
    expected_positions = {
        item.instrument_id: item.reserved_quantity
        for item in command.snapshot.positions
    }
    valid = valid and _without_zeros(cash_reserved) == _without_zeros(expected_cash)
    valid = valid and _without_zeros(position_reserved) == _without_zeros(
        expected_positions
    )
    valid = valid and order_ids == set(command.snapshot.pending_order_ids)
    return _check(
        "reservation_reconciliation",
        valid,
        actual=Decimal(len(command.open_reservations)),
        limit=Decimal(len(command.snapshot.pending_order_ids)),
        reason="open reservations do not match reserved projections and pending orders",
    )


def _cash_check(state: _EvaluationState) -> RiskCheck:
    required = state.command.target.expected_cost
    missing = False
    for allocation in state.command.target.allocations:
        if allocation.delta_quantity <= 0:
            continue
        instrument = state.instruments.get(allocation.instrument_id)
        if instrument is None:
            missing = True
            continue
        required += allocation.delta_quantity * instrument.mark_price
    available = state.cash.available_amount if state.cash is not None else ZERO
    return _check(
        "cash_available",
        not missing and state.cash is not None and required <= available,
        actual=required,
        limit=available,
        reason="base-currency available cash is insufficient or unknown",
    )


def _position_check(state: _EvaluationState) -> RiskCheck:
    shortfall = ZERO
    for allocation in state.command.target.allocations:
        if allocation.delta_quantity >= 0:
            continue
        position = state.positions.get(allocation.instrument_id)
        available = position.available_quantity if position is not None else ZERO
        shortfall = max(shortfall, abs(allocation.delta_quantity) - available)
    return _check(
        "position_available",
        shortfall <= 0,
        actual=max(ZERO, shortfall),
        limit=ZERO,
        reason="sell quantity exceeds available position after reservations",
    )


def _pending_orders_check(
    command: BuildRiskDecisionCommand,
    policy: HardRiskPolicy,
) -> RiskCheck:
    planned = sum(item.delta_quantity != 0 for item in command.target.allocations)
    count = len(command.snapshot.pending_order_ids) + planned
    return _check(
        "pending_orders",
        count <= policy.max_pending_orders,
        actual=Decimal(count),
        limit=Decimal(policy.max_pending_orders),
        reason="pending plus planned orders exceed policy limit",
    )


def _adv_check(state: _EvaluationState, policy: HardRiskPolicy) -> RiskCheck:
    maximum = ZERO
    missing = False
    for allocation in state.command.target.allocations:
        instrument = state.instruments.get(allocation.instrument_id)
        if instrument is None:
            missing = True
            continue
        maximum = max(
            maximum,
            abs(allocation.delta_quantity) / instrument.average_daily_volume,
        )
    return _check(
        "adv_participation",
        not missing and maximum <= policy.max_adv_participation,
        actual=_quantize(maximum),
        limit=policy.max_adv_participation,
        reason="planned quantity exceeds ADV participation limit or ADV is unknown",
    )


def _market_session_check(
    state: _EvaluationState,
    policy: HardRiskPolicy,
) -> RiskCheck:
    if not policy.require_market_open:
        return _check("market_session", True, actual=ZERO, limit=ZERO)
    closed = sum(
        1
        for allocation in state.command.target.allocations
        if allocation.delta_quantity != 0
        and (
            allocation.instrument_id not in state.instruments
            or not state.instruments[allocation.instrument_id].session.is_open_at(
                state.command.at
            )
        )
    )
    return _check(
        "market_session",
        closed == 0,
        actual=Decimal(closed),
        limit=ZERO,
        reason="one or more target instruments are outside an open market session",
    )


def _single_position_check(
    command: BuildRiskDecisionCommand,
    policy: HardRiskPolicy,
) -> RiskCheck:
    maximum = max(
        (abs(item.target_weight) for item in command.target.allocations), default=ZERO
    )
    return _check(
        "single_position",
        maximum <= policy.max_single_position_weight,
        actual=maximum,
        limit=policy.max_single_position_weight,
        reason="single-instrument target weight exceeds policy limit",
    )


def _sector_check(state: _EvaluationState, policy: HardRiskPolicy) -> RiskCheck:
    exposures: defaultdict[str, Decimal] = defaultdict(Decimal)
    missing = False
    for allocation in state.command.target.allocations:
        instrument = state.instruments.get(allocation.instrument_id)
        if instrument is None:
            missing = True
            continue
        exposures[instrument.sector] += abs(allocation.target_weight)
    maximum = max(exposures.values(), default=ZERO)
    return _check(
        "sector_exposure",
        not missing and maximum <= policy.max_sector_weight,
        actual=maximum,
        limit=policy.max_sector_weight,
        reason="sector exposure exceeds policy limit or sector is unknown",
    )


def _asset_class_check(
    state: _EvaluationState,
    policy: HardRiskPolicy,
) -> RiskCheck:
    exposures: defaultdict[object, Decimal] = defaultdict(Decimal)
    unsupported = False
    for allocation in state.command.target.allocations:
        instrument = state.instruments.get(allocation.instrument_id)
        if instrument is None:
            unsupported = True
            continue
        unsupported = (
            unsupported or instrument.asset_class not in policy.allowed_asset_classes
        )
        exposures[instrument.asset_class] += abs(allocation.target_weight)
    maximum = max(exposures.values(), default=ZERO)
    return _check(
        "asset_class",
        not unsupported and maximum <= policy.max_asset_class_weight,
        actual=maximum,
        limit=policy.max_asset_class_weight,
        reason="asset class is unsupported, unknown, or over its exposure limit",
    )


def _gross_check(state: _EvaluationState, policy: HardRiskPolicy) -> RiskCheck:
    gross = sum(
        (abs(item.target_weight) for item in state.command.target.allocations), ZERO
    )
    return _check(
        "gross_exposure",
        gross <= policy.max_gross_exposure,
        actual=gross,
        limit=policy.max_gross_exposure,
        reason="gross exposure exceeds policy limit",
    )


def _net_check(state: _EvaluationState, policy: HardRiskPolicy) -> RiskCheck:
    net = sum((item.target_weight for item in state.command.target.allocations), ZERO)
    passed = policy.min_net_exposure <= net <= policy.max_net_exposure
    return _check(
        "net_exposure",
        passed,
        actual=net,
        limit=policy.max_net_exposure,
        reason="net exposure is outside policy range",
    )


def _turnover_check(
    command: BuildRiskDecisionCommand,
    policy: HardRiskPolicy,
) -> RiskCheck:
    return _check(
        "turnover",
        command.target.expected_turnover <= policy.max_turnover,
        actual=command.target.expected_turnover,
        limit=policy.max_turnover,
        reason="expected turnover exceeds policy limit",
    )


def _drawdown_check(state: _EvaluationState, policy: HardRiskPolicy) -> RiskCheck:
    drawdown = _loss_ratio(state.nav, state.command.high_watermark_nav)
    return _check(
        "drawdown",
        drawdown is not None and drawdown <= policy.max_drawdown,
        actual=drawdown,
        limit=policy.max_drawdown,
        reason="current drawdown exceeds policy limit or NAV is unknown",
    )


def _daily_loss_check(state: _EvaluationState, policy: HardRiskPolicy) -> RiskCheck:
    daily_loss = _loss_ratio(state.nav, state.command.day_start_nav)
    return _check(
        "daily_loss",
        daily_loss is not None and daily_loss <= policy.max_daily_loss,
        actual=daily_loss,
        limit=policy.max_daily_loss,
        reason="daily loss exceeds policy limit or NAV is unknown",
    )


def _loss_ratio(current: Decimal | None, reference: Decimal) -> Decimal | None:
    if current is None:
        return None
    return _quantize(max(ZERO, (reference - current) / reference))


def _check(
    code: str,
    passed: bool,
    *,
    actual: Decimal | None = None,
    limit: Decimal | None = None,
    reason: str | None = None,
) -> RiskCheck:
    return RiskCheck(
        code=code,
        passed=passed,
        actual=actual,
        limit=limit,
        reason=None if passed else reason,
    )


def _without_zeros[T](values: dict[T, Decimal]) -> dict[T, Decimal]:
    return {key: value for key, value in values.items() if value != 0}


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(CALCULATION_QUANTUM, rounding=ROUND_HALF_EVEN)
