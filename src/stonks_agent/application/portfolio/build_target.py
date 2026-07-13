"""Versioned deterministic portfolio construction with fail-closed inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from uuid import UUID

import yaml

from stonks_agent.domain._trading import failure
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    PortfolioTarget,
    PositionBalance,
    TargetAllocation,
)
from stonks_agent.domain.portfolio_construction import (
    BuildTargetCommand,
    PortfolioMark,
    PortfolioPolicy,
    PortfolioSignalCandidate,
)
from stonks_agent.domain.signal import evaluate_signal_eligibility

CALCULATION_QUANTUM = Decimal("0.000000000001")
ONE = Decimal(1)
ZERO = Decimal(0)


def load_portfolio_policy(path: str | Path) -> PortfolioPolicy:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return PortfolioPolicy.model_validate(payload)
    except (OSError, yaml.YAMLError, TypeError) as error:
        raise ValueError("portfolio policy could not be loaded") from error


@dataclass(frozen=True, slots=True)
class DeterministicPortfolioBuilder:
    policy: PortfolioPolicy

    def build_target(self, command: BuildTargetCommand) -> Result[PortfolioTarget]:
        return build_target(command, self.policy)


def build_target(
    command: BuildTargetCommand,
    policy: PortfolioPolicy,
) -> Result[PortfolioTarget]:
    validated = _validated_inputs(command, policy)
    if isinstance(validated, Failure):
        return validated
    marks, candidates = validated.value
    position_map = {item.instrument_id: item for item in command.snapshot.positions}
    instrument_ids = tuple(
        sorted(
            set(position_map) | {item.signal.instrument_id for item in candidates},
            key=str,
        )
    )
    if not instrument_ids:
        return failure(ErrorCode.DATA_UNAVAILABLE, "No portfolio instruments exist")
    missing = tuple(value for value in instrument_ids if value not in marks)
    if missing:
        return failure(
            ErrorCode.DATA_UNAVAILABLE,
            "A required portfolio mark is unavailable",
            instrument_ids=tuple(str(value) for value in missing),
        )
    nav = _portfolio_nav(command.snapshot, marks, command.base_currency)
    if isinstance(nav, Failure):
        return nav
    allocations = tuple(
        _allocation(
            instrument_id,
            position_map.get(instrument_id),
            marks[instrument_id],
            candidates,
            nav.value,
            policy,
        )
        for instrument_id in instrument_ids
    )
    turnover, cost = _target_costs(allocations, marks, nav.value, policy)
    return Success(
        PortfolioTarget.create(
            target_id=command.target_id,
            account_id=command.snapshot.account_id,
            portfolio_snapshot_id=command.snapshot.snapshot_id,
            account_aggregate_sequence=command.snapshot.account_aggregate_sequence,
            portfolio_sequence=command.snapshot.portfolio_sequence,
            as_of=command.snapshot.as_of,
            allocations=allocations,
            input_signal_ids=tuple(
                sorted((item.signal.signal_id for item in candidates), key=str)
            ),
            policy_version=policy.policy_version,
            policy_hash=policy.policy_hash,
            expected_turnover=turnover,
            expected_cost=cost,
            cost_currency=command.base_currency,
        )
    )


def _validated_inputs(
    command: BuildTargetCommand,
    policy: PortfolioPolicy,
) -> Result[tuple[dict[UUID, PortfolioMark], tuple[PortfolioSignalCandidate, ...]]]:
    currency_error = _currency_error(command, policy)
    if currency_error is not None:
        return currency_error
    marks = {item.instrument_id: item for item in command.marks}
    if len(marks) != len(command.marks):
        return failure(ErrorCode.CONFLICT, "Portfolio marks contain duplicates")
    if any(item.as_of > command.snapshot.as_of for item in command.marks):
        return failure(ErrorCode.INVALID_INPUT, "Portfolio mark is from the future")
    candidates = tuple(
        sorted(command.signal_candidates, key=lambda item: str(item.signal.signal_id))
    )
    candidate_error = _candidate_error(candidates, command.snapshot.as_of, policy)
    if candidate_error is not None:
        return candidate_error
    quantum_error = _position_quantum_error(command.snapshot.positions, marks)
    if quantum_error is not None:
        return quantum_error
    return Success((marks, candidates))


def _currency_error(
    command: BuildTargetCommand,
    policy: PortfolioPolicy,
) -> Failure | None:
    cash = command.snapshot.cash
    if len(cash) != 1 or cash[0].currency != command.base_currency:
        return failure(
            ErrorCode.CONFLICT,
            "Portfolio baseline requires one base-currency cash balance",
        )
    if cash[0].quantum != policy.currency_quantum:
        return failure(ErrorCode.CONFLICT, "Cash and policy currency quantum differ")
    if any(item.currency != command.base_currency for item in command.marks):
        return failure(ErrorCode.CONFLICT, "Portfolio mark currency differs from base")
    return None


def _candidate_error(
    candidates: tuple[PortfolioSignalCandidate, ...],
    at: datetime,
    policy: PortfolioPolicy,
) -> Failure | None:
    signal_ids = tuple(item.signal.signal_id for item in candidates)
    keys = tuple(
        (
            item.signal.instrument_id,
            item.signal.strategy_id,
            item.signal.strategy_version,
        )
        for item in candidates
    )
    if len(signal_ids) != len(set(signal_ids)) or len(keys) != len(set(keys)):
        return failure(ErrorCode.CONFLICT, "Portfolio signals contain duplicates")
    configured = {item.key for item in policy.strategy_weights}
    for candidate in candidates:
        signal = candidate.signal
        if signal.generated_at > at or signal.as_of > at:
            return failure(
                ErrorCode.INVALID_INPUT, "Portfolio signal is from the future"
            )
        if candidate.registry.updated_at > at or candidate.evaluation.created_at > at:
            return failure(
                ErrorCode.INVALID_INPUT,
                "Portfolio signal eligibility binding is from the future",
            )
        if (signal.strategy_id, signal.strategy_version) not in configured:
            return failure(
                ErrorCode.CONFIGURATION_INVALID,
                "Portfolio signal has no configured fixed weight",
            )
        decision = evaluate_signal_eligibility(
            signal,
            registry=candidate.registry,
            evaluation=candidate.evaluation,
            at=at,
        )
        if not decision.eligible:
            return failure(
                ErrorCode.CONFLICT,
                "Portfolio signal is not eligible",
                signal_id=str(signal.signal_id),
                reason=decision.reason_codes[0],
            )
    return None


def _position_quantum_error(
    positions: tuple[PositionBalance, ...],
    marks: dict[UUID, PortfolioMark],
) -> Failure | None:
    for position in positions:
        mark = marks.get(position.instrument_id)
        if mark is not None and mark.quantity_quantum != position.quantum:
            return failure(
                ErrorCode.CONFLICT,
                "Position and mark quantity quantum differ",
                instrument_id=str(position.instrument_id),
            )
    return None


def _portfolio_nav(
    snapshot: AccountPortfolioSnapshot,
    marks: dict[UUID, PortfolioMark],
    base_currency: str,
) -> Result[Decimal]:
    cash = next(item for item in snapshot.cash if item.currency == base_currency)
    missing = tuple(
        item for item in snapshot.positions if item.instrument_id not in marks
    )
    if missing:
        return failure(
            ErrorCode.DATA_UNAVAILABLE,
            "A current position mark is unavailable",
            instrument_ids=tuple(str(item.instrument_id) for item in missing),
        )
    nav = cash.settled_amount + sum(
        (
            item.quantity * marks[item.instrument_id].price
            for item in snapshot.positions
        ),
        ZERO,
    )
    if nav <= 0:
        return failure(ErrorCode.DATA_UNAVAILABLE, "Portfolio NAV must be positive")
    return Success(nav)


def _allocation(
    instrument_id: UUID,
    position: PositionBalance | None,
    mark: PortfolioMark,
    candidates: tuple[PortfolioSignalCandidate, ...],
    nav: Decimal,
    policy: PortfolioPolicy,
) -> TargetAllocation:
    current_quantity = position.quantity if position is not None else ZERO
    current_weight = current_quantity * mark.price / nav
    instrument_candidates = tuple(
        item for item in candidates if item.signal.instrument_id == instrument_id
    )
    score, available_weight = _ensemble_score(instrument_candidates, policy)
    deadband_applied = abs(score) <= policy.deadband
    desired_weight = ZERO if deadband_applied else score * policy.shrinkage
    penalized_weight = current_weight + (
        (desired_weight - current_weight) * (ONE - policy.turnover_penalty)
    )
    bounded_weight = min(
        policy.max_position_weight,
        max(ZERO, penalized_weight),
    )
    target_quantity = _floor_to_quantum(
        bounded_weight * nav / mark.price,
        mark.quantity_quantum,
    )
    target_weight = _quantize(target_quantity * mark.price / nav)
    delta_quantity = target_quantity - current_quantity
    estimated_cost = _currency_quantize(
        abs(delta_quantity) * mark.price * policy.estimated_cost_bps / Decimal("10000"),
        policy.currency_quantum,
    )
    diagnostics = _diagnostics(
        score=score,
        available_weight=available_weight,
        penalized_weight=penalized_weight,
        bounded_weight=bounded_weight,
        deadband_applied=deadband_applied,
        estimated_cost=estimated_cost,
        mark=mark,
        policy=policy,
    )
    return TargetAllocation(
        instrument_id=instrument_id,
        current_quantity=current_quantity,
        target_quantity=target_quantity,
        delta_quantity=delta_quantity,
        quantity_quantum=mark.quantity_quantum,
        target_weight=target_weight,
        constraint_diagnostics=diagnostics,
    )


def _ensemble_score(
    candidates: tuple[PortfolioSignalCandidate, ...],
    policy: PortfolioPolicy,
) -> tuple[Decimal, Decimal]:
    weights = {item.key: item.weight for item in policy.strategy_weights}
    score = sum(
        (
            weights[(item.signal.strategy_id, item.signal.strategy_version)]
            * item.signal.value
            * item.signal.confidence
            for item in candidates
        ),
        ZERO,
    )
    available = sum(
        (
            weights[(item.signal.strategy_id, item.signal.strategy_version)]
            for item in candidates
        ),
        ZERO,
    )
    return _quantize(score), _quantize(available)


def _diagnostics(
    *,
    score: Decimal,
    available_weight: Decimal,
    penalized_weight: Decimal,
    bounded_weight: Decimal,
    deadband_applied: bool,
    estimated_cost: Decimal,
    mark: PortfolioMark,
    policy: PortfolioPolicy,
) -> tuple[str, ...]:
    values = (
        f"deadband:{'applied' if deadband_applied else 'not_applied'}",
        f"ensemble_available_weight:{_fixed(available_weight)}",
        f"ensemble_missing_weight:{_fixed(ONE - available_weight)}",
        f"ensemble_score:{_fixed(score)}",
        f"estimated_cost:{format(estimated_cost, 'f')}",
        f"estimated_cost_bps:{format(policy.estimated_cost_bps, 'f')}",
        f"mark_price:{format(mark.price, 'f')}",
        f"position_bound:{'applied' if bounded_weight != penalized_weight else 'not_applied'}",
        f"shrinkage:{format(policy.shrinkage, 'f')}",
        f"turnover_penalty:{format(policy.turnover_penalty, 'f')}",
    )
    return tuple(sorted(values))


def _target_costs(
    allocations: tuple[TargetAllocation, ...],
    marks: dict[UUID, PortfolioMark],
    nav: Decimal,
    policy: PortfolioPolicy,
) -> tuple[Decimal, Decimal]:
    traded_notional = sum(
        (
            abs(item.delta_quantity) * marks[item.instrument_id].price
            for item in allocations
        ),
        ZERO,
    )
    turnover = _quantize(traded_notional / nav)
    cost = _currency_quantize(
        traded_notional * policy.estimated_cost_bps / Decimal("10000"),
        policy.currency_quantum,
    )
    return turnover, cost


def _floor_to_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    units = (value / quantum).to_integral_value(rounding=ROUND_FLOOR)
    return units * quantum


def _currency_quantize(value: Decimal, quantum: Decimal) -> Decimal:
    units = (value / quantum).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return units * quantum


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(CALCULATION_QUANTUM, rounding=ROUND_HALF_EVEN)


def _fixed(value: Decimal) -> str:
    return format(_quantize(value), "f")
