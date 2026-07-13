"""Authorized strategy reads, CAS transitions, and signal eligibility checks."""

from __future__ import annotations

from uuid import UUID

from stonks_agent.domain.auth import LocalPrincipal, Permission, authorize
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evaluation import EvaluationReport
from stonks_agent.domain.signal import (
    AlphaSignal,
    SignalEligibilityDecision,
    evaluate_signal_eligibility,
)
from stonks_agent.domain.strategy import (
    StrategyAuditEvent,
    StrategyMutationResult,
    StrategyRegistryEntry,
    StrategyTransitionRequest,
)
from stonks_agent.ports.strategy_registry import (
    StrategyRegistryPort,
    StrategyUnitOfWorkFactory,
)
from stonks_contracts.common import UTCDateTime


def read_strategy(
    principal: LocalPrincipal,
    strategy_id: str,
    strategy_version: str,
    unit_of_work: StrategyUnitOfWorkFactory,
) -> Result[StrategyRegistryEntry]:
    denied = _authorize(principal, Permission.READ)
    if denied is not None:
        return denied
    with unit_of_work() as transaction:
        return transaction.strategies.get(strategy_id, strategy_version)


def read_strategy_events(
    principal: LocalPrincipal,
    strategy_id: str,
    strategy_version: str,
    unit_of_work: StrategyUnitOfWorkFactory,
) -> Result[tuple[StrategyAuditEvent, ...]]:
    denied = _authorize(principal, Permission.READ)
    if denied is not None:
        return denied
    with unit_of_work() as transaction:
        return transaction.strategies.list_events(strategy_id, strategy_version)


def read_evaluation(
    principal: LocalPrincipal,
    report_id: UUID,
    unit_of_work: StrategyUnitOfWorkFactory,
) -> Result[EvaluationReport]:
    denied = _authorize(principal, Permission.READ)
    if denied is not None:
        return denied
    with unit_of_work() as transaction:
        return transaction.strategies.get_evaluation(report_id)


def transition_strategy(
    principal: LocalPrincipal,
    request: StrategyTransitionRequest,
    unit_of_work: StrategyUnitOfWorkFactory,
) -> Result[StrategyMutationResult]:
    denied = _authorize(principal, Permission.REVIEW_STRATEGY)
    if denied is not None:
        return denied
    if request.actor != principal.subject:
        return Failure(_denied_actor())
    with unit_of_work() as transaction:
        result = transaction.strategies.transition(request)
        if isinstance(result, Success):
            transaction.commit()
        return result


def check_signal_eligibility(
    principal: LocalPrincipal,
    signal: AlphaSignal,
    *,
    at: UTCDateTime,
    unit_of_work: StrategyUnitOfWorkFactory,
) -> Result[SignalEligibilityDecision]:
    denied = _authorize(principal, Permission.READ)
    if denied is not None:
        return denied
    with unit_of_work() as transaction:
        registry = transaction.strategies.get(
            signal.strategy_id, signal.strategy_version
        )
        if isinstance(registry, Failure):
            if registry.error.code is ErrorCode.NOT_FOUND:
                return Success(
                    evaluate_signal_eligibility(
                        signal, registry=None, evaluation=None, at=at
                    )
                )
            return registry
        evaluation = _signal_evaluation(signal, transaction.strategies)
        if isinstance(evaluation, Failure):
            return evaluation
        return Success(
            evaluate_signal_eligibility(
                signal,
                registry=registry.value,
                evaluation=evaluation.value,
                at=at,
            )
        )


def _signal_evaluation(
    signal: AlphaSignal, repository: StrategyRegistryPort
) -> Result[EvaluationReport | None]:
    if signal.evaluation_report_id is None:
        return Success(None)
    result = repository.get_evaluation(signal.evaluation_report_id)
    if isinstance(result, Failure) and result.error.code is ErrorCode.NOT_FOUND:
        return Success(None)
    return result


def _authorize(principal: LocalPrincipal, permission: Permission) -> Failure | None:
    result = authorize(principal, permission)
    return result if isinstance(result, Failure) else None


def _denied_actor() -> StructuredError:
    return StructuredError(
        code=ErrorCode.FORBIDDEN,
        message="Strategy transition actor must match authenticated principal",
    )
