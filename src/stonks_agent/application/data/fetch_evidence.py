"""Policy-owned provider fallback without arbitrary provider or URL input."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field

from stonks_agent.domain.data_quality import (
    ProviderDataState,
    ProviderHealthState,
    ProviderObservation,
    ProviderRuntimeHealth,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.provider_policy import (
    ProviderPolicy,
    ProviderRoute,
    ReconciliationStrategy,
    ReconciliationValue,
    reconcile_comparable_values,
)
from stonks_contracts.common import UTCDateTime


class FetchDataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    as_of: UTCDateTime
    query: dict[str, object]


class ProviderAdapter(Protocol):
    def fetch(self, request: FetchDataRequest) -> object: ...


class FetchedProviderData[T](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    observation: ProviderObservation[T]
    attempted_states: tuple[tuple[str, ProviderDataState], ...]


@dataclass(frozen=True, slots=True)
class _Candidate[T]:
    provider: str
    observation: ProviderObservation[T]


def _log_boundary_failure(provider: str, stage: str, error: Exception) -> None:
    structlog.get_logger(__name__).warning(
        "provider_policy_boundary_failed",
        provider=provider,
        stage=stage,
        error_type=type(error).__name__,
    )


def _runtime_gate(
    request: FetchDataRequest,
    route: ProviderRoute,
    health: ProviderRuntimeHealth | None,
    *,
    allow_stale: bool,
) -> tuple[ProviderDataState | None, bool]:
    if health is None:
        if route.freshness_seconds > 0:
            return ProviderDataState.FRESHNESS_UNKNOWN, False
        if route.quota_floor > 0:
            return ProviderDataState.QUOTA_UNKNOWN, False
        return None, False
    if health.checked_at > request.as_of:
        return ProviderDataState.CONFLICT, False
    if health.state is ProviderHealthState.UNKNOWN:
        return ProviderDataState.HEALTH_UNKNOWN, False
    if health.state is ProviderHealthState.UNAVAILABLE:
        return ProviderDataState.PROVIDER_UNHEALTHY, False
    if route.quota_floor > 0:
        if health.remaining_quota is None:
            return ProviderDataState.QUOTA_UNKNOWN, False
        if health.remaining_quota < route.quota_floor:
            return ProviderDataState.QUOTA_EXHAUSTED, False
    if route.freshness_seconds == 0:
        return None, False
    if health.latest_data_at is None:
        return ProviderDataState.FRESHNESS_UNKNOWN, False
    if health.latest_data_at > request.as_of:
        return ProviderDataState.CONFLICT, False
    age_seconds = (request.as_of - health.latest_data_at).total_seconds()
    if age_seconds > route.freshness_seconds:
        if allow_stale:
            return None, True
        return ProviderDataState.STALE, False
    return None, False


def _mark_runtime_stale[T](
    observation: ProviderObservation[T],
) -> ProviderObservation[T] | None:
    if observation.state is ProviderDataState.STALE:
        return observation
    if observation.state is not ProviderDataState.AVAILABLE:
        return None
    return observation.model_copy(
        update={
            "state": ProviderDataState.STALE,
            "reasons": (*observation.reasons, "route_freshness_exceeded"),
        }
    )


def _fetch_candidate[T](
    request: FetchDataRequest,
    route: ProviderRoute,
    adapter: ProviderAdapter,
    health: ProviderRuntimeHealth | None,
    policy: ProviderPolicy,
) -> tuple[ProviderDataState, _Candidate[T] | None]:
    runtime_state, runtime_stale = _runtime_gate(
        request,
        route,
        health,
        allow_stale=policy.allow_stale,
    )
    if runtime_state is not None:
        return runtime_state, None
    try:
        raw_observation = adapter.fetch(request)
    except Exception as error:
        _log_boundary_failure(route.provider, "fetch", error)
        return ProviderDataState.FETCH_FAILED, None
    if not isinstance(raw_observation, ProviderObservation):
        return ProviderDataState.FETCH_FAILED, None
    observation = cast(ProviderObservation[T], raw_observation)
    if runtime_stale:
        stale_observation = _mark_runtime_stale(observation)
        if stale_observation is None:
            return ProviderDataState.STALE, None
        observation = stale_observation
    if not observation.accepted(
        allow_stale=policy.allow_stale,
        allow_partial=policy.allow_partial,
    ):
        return observation.state, None
    return observation.state, _Candidate(
        provider=route.provider,
        observation=observation,
    )


def _success[T](
    candidate: _Candidate[T],
    attempted: list[tuple[str, ProviderDataState]],
) -> Success[FetchedProviderData[T]]:
    return Success(
        FetchedProviderData[T](
            provider=candidate.provider,
            observation=candidate.observation,
            attempted_states=tuple(attempted),
        )
    )


def _unavailable(
    attempted: list[tuple[str, ProviderDataState]],
) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="No provider produced policy-acceptable data",
            details={
                "attempted_states": tuple(
                    (provider, state.value) for provider, state in attempted
                )
            },
        )
    )


def _reconciliation_failure(
    attempted: list[tuple[str, ProviderDataState]],
    providers: tuple[str, str],
    *,
    reason: str,
    relative_difference: str | None = None,
) -> Failure:
    details: dict[str, object] = {
        "attempted_states": tuple(
            (provider, state.value) for provider, state in attempted
        ),
        "reconciliation_state": ProviderDataState.CONFLICT.value,
        "providers": providers,
        "reason": reason,
    }
    if relative_difference is not None:
        details["relative_difference"] = relative_difference
    return Failure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="Provider reconciliation failed closed",
            details=details,
        )
    )


def _reconcile_candidates[T](
    primary: _Candidate[T],
    secondary: _Candidate[T],
    *,
    policy: ProviderPolicy,
    strategy: ReconciliationStrategy[T],
    attempted: list[tuple[str, ProviderDataState]],
) -> Failure | None:
    providers = (primary.provider, secondary.provider)
    primary_empty = primary.observation.state is ProviderDataState.LEGITIMATE_EMPTY
    secondary_empty = secondary.observation.state is ProviderDataState.LEGITIMATE_EMPTY
    if primary_empty and secondary_empty:
        return None
    if primary_empty or secondary_empty:
        return _reconciliation_failure(
            attempted, providers, reason="reconciliation_empty_mismatch"
        )
    primary_value, primary_failed = _extract_reconciliation_value(primary, strategy)
    if primary_failed:
        return _reconciliation_failure(
            attempted, providers, reason="reconciliation_strategy_failed"
        )
    secondary_value, secondary_failed = _extract_reconciliation_value(
        secondary, strategy
    )
    if secondary_failed:
        return _reconciliation_failure(
            attempted, providers, reason="reconciliation_strategy_failed"
        )
    if primary_value is None or secondary_value is None:
        return _reconciliation_failure(
            attempted, providers, reason="reconciliation_value_unavailable"
        )
    decision = reconcile_comparable_values(primary_value, secondary_value, policy)
    if decision.state is ProviderDataState.CONFLICT:
        return _reconciliation_failure(
            attempted,
            providers,
            reason=decision.reasons[0],
            relative_difference=str(decision.relative_difference),
        )
    return None


def _extract_reconciliation_value[T](
    candidate: _Candidate[T],
    strategy: ReconciliationStrategy[T],
) -> tuple[ReconciliationValue | None, bool]:
    try:
        return strategy.extract(candidate.provider, candidate.observation), False
    except Exception as error:
        _log_boundary_failure(candidate.provider, "reconciliation_extract", error)
        return None, True


def fetch_provider_data[T](
    request: FetchDataRequest,
    *,
    policy: ProviderPolicy,
    adapters: Mapping[str, ProviderAdapter],
    runtime_health: Mapping[str, ProviderRuntimeHealth] | None = None,
    reconciliation_strategy: ReconciliationStrategy[T] | None = None,
) -> Result[FetchedProviderData[T]]:
    if (request.market, request.capability) != (policy.market, policy.capability):
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Provider policy does not match the requested capability",
            )
        )
    attempted: list[tuple[str, ProviderDataState]] = []
    candidates: list[_Candidate[T]] = []
    health_by_provider = runtime_health or {}
    for route in policy.routes:
        adapter = adapters.get(route.provider)
        if adapter is None:
            attempted.append((route.provider, ProviderDataState.CONFIG_MISSING))
            continue
        outcome: tuple[ProviderDataState, _Candidate[T] | None] = _fetch_candidate(
            request,
            route,
            adapter,
            health_by_provider.get(route.provider),
            policy,
        )
        state, candidate = outcome
        attempted.append((route.provider, state))
        if candidate is None:
            continue
        if reconciliation_strategy is None:
            return _success(candidate, attempted)
        candidates.append(candidate)
        if len(candidates) == 2:
            break
    if not candidates:
        return _unavailable(attempted)
    primary = candidates[0]
    if len(candidates) == 1 or reconciliation_strategy is None:
        return _success(primary, attempted)
    conflict = _reconcile_candidates(
        primary,
        candidates[1],
        policy=policy,
        strategy=reconciliation_strategy,
        attempted=attempted,
    )
    if conflict is not None:
        return conflict
    return _success(primary, attempted)
