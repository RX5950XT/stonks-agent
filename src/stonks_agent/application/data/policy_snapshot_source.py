"""Policy-owned snapshot provider routing and reconciliation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from pydantic import ValidationError

from stonks_agent.application.data.fetch_evidence import (
    FetchDataRequest,
    FetchedProviderData,
    ProviderAdapter,
    fetch_provider_data,
)
from stonks_agent.domain.data_quality import (
    ProviderDataState,
    ProviderObservation,
    ProviderRuntimeHealth,
)
from stonks_agent.domain.dataset_snapshot import (
    ProviderSnapshotMaterialization,
    ReconciliationCandidateTrace,
    ReconciliationTrace,
    normalized_evidence_content_hash,
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
    ReconciliationOutcome,
    ReconciliationStrategy,
    ReconciliationValue,
    reconcile_comparable_values,
)
from stonks_agent.ports.snapshot_materialization import SnapshotMaterializationSource
from stonks_contracts.common import stable_payload_hash


class PolicySnapshotMaterializationSource:
    """Select immutable provider output through one core-owned policy."""

    def __init__(
        self,
        *,
        policy: ProviderPolicy,
        sources: Mapping[str, SnapshotMaterializationSource[FetchDataRequest]],
        reconciliation_strategy: ReconciliationStrategy[object],
        runtime_health: Mapping[str, ProviderRuntimeHealth] | None = None,
    ) -> None:
        self._policy = policy
        self._sources = dict(sources)
        self._strategy = reconciliation_strategy
        self._runtime_health = dict(runtime_health or {})

    def fetch(
        self,
        request: FetchDataRequest,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        if not _request_is_authorized(request, provider_policy_id, self._policy):
            return _failure(
                ErrorCode.CAPABILITY_DENIED,
                "Provider policy does not authorize snapshot request",
            )
        materializations: dict[str, ProviderSnapshotMaterialization] = {}
        route_violations: set[str] = set()
        trace_strategy = _TraceCapturingStrategy(self._strategy, {})
        selected: Result[FetchedProviderData[object]] = fetch_provider_data(
            request,
            policy=self._policy,
            adapters=self._adapters(
                provider_policy_id,
                materializations,
                route_violations,
            ),
            runtime_health=self._runtime_health,
            reconciliation_strategy=trace_strategy,
        )
        if route_violations:
            return _failure(
                ErrorCode.CAPABILITY_DENIED,
                "Snapshot source returned an unauthorized provider route",
            )
        return _result_with_core_trace(
            selected,
            materializations,
            trace_strategy.values,
            self._policy,
        )

    def _adapters(
        self,
        provider_policy_id: str,
        materializations: dict[str, ProviderSnapshotMaterialization],
        route_violations: set[str],
    ) -> Mapping[str, ProviderAdapter]:
        return {
            route.provider: _SnapshotSourceAdapter(
                route=route,
                source=source,
                provider_policy_id=provider_policy_id,
                materializations=materializations,
                route_violations=route_violations,
            )
            for route in self._policy.routes
            if (source := self._sources.get(route.provider)) is not None
        }


@dataclass(frozen=True, slots=True)
class _SnapshotSourceAdapter:
    route: ProviderRoute
    source: SnapshotMaterializationSource[FetchDataRequest]
    provider_policy_id: str
    materializations: dict[str, ProviderSnapshotMaterialization]
    route_violations: set[str]

    def fetch(self, request: FetchDataRequest) -> ProviderObservation[object]:
        result = self.source.fetch(
            request,
            provider_policy_id=self.provider_policy_id,
        )
        if isinstance(result, Failure):
            return _rejected_observation(
                request,
                ProviderDataState.FETCH_FAILED,
                "snapshot_source_failed",
            )
        value = result.value
        if value.reconciliation_trace is not None or not _route_authorizes(
            value, self.route
        ):
            self.route_violations.add(self.route.provider)
            return _rejected_observation(
                request,
                ProviderDataState.CONFLICT,
                "snapshot_source_route_unauthorized",
            )
        self.materializations[self.route.provider] = value
        return _canonical_observation(value)


@dataclass(frozen=True, slots=True)
class _TraceCapturingStrategy:
    delegate: ReconciliationStrategy[object]
    values: dict[str, ReconciliationValue]

    def extract(
        self,
        provider: str,
        observation: ProviderObservation[object],
    ) -> ReconciliationValue | None:
        extracted = self.delegate.extract(provider, observation)
        if extracted is None:
            return None
        canonical = ReconciliationValue.model_validate(
            extracted.model_dump(mode="python")
        )
        existing = self.values.get(provider)
        if existing is not None and existing != canonical:
            raise ValueError("reconciliation strategy changed a captured value")
        self.values[provider] = canonical
        return canonical


def _request_is_authorized(
    request: FetchDataRequest,
    provider_policy_id: str,
    policy: ProviderPolicy,
) -> bool:
    return (
        provider_policy_id == policy.policy_id
        and request.market == policy.market
        and request.capability == policy.capability
    )


def _route_authorizes(
    value: ProviderSnapshotMaterialization,
    route: ProviderRoute,
) -> bool:
    return value.provider == route.provider and value.endpoint in route.endpoints


def _canonical_observation(
    value: ProviderSnapshotMaterialization,
) -> ProviderObservation[object]:
    data = tuple(
        json.loads(
            json.dumps(
                item.payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        for item in value.evidence
    )
    return ProviderObservation[object](
        state=value.observation.state,
        data=data,
        completeness=value.observation.completeness,
        reasons=value.observation.reasons,
        observed_at=value.observation.observed_at,
    )


def _rejected_observation(
    request: FetchDataRequest,
    state: ProviderDataState,
    reason: str,
) -> ProviderObservation[object]:
    return ProviderObservation[object](
        state=state,
        data=(),
        completeness=Decimal("0"),
        reasons=(reason,),
        observed_at=request.as_of,
    )


def _selected_materialization(
    selected: FetchedProviderData[object],
    materializations: Mapping[str, ProviderSnapshotMaterialization],
    trace: ReconciliationTrace | None,
) -> Result[ProviderSnapshotMaterialization]:
    value = materializations.get(selected.provider)
    if value is None:
        return _failure(
            ErrorCode.CONFLICT,
            "Selected snapshot provider output is unavailable",
        )
    try:
        canonical = ProviderSnapshotMaterialization.model_validate(
            value.model_copy(
                update={
                    "observation": selected.observation,
                    "reconciliation_trace": trace,
                }
            ).model_dump(mode="python")
        )
    except (TypeError, ValueError, ValidationError):
        return _failure(ErrorCode.INVALID_INPUT, "Selected snapshot output is invalid")
    return Success(canonical)


def _result_with_core_trace(
    selected: Result[FetchedProviderData[object]],
    materializations: Mapping[str, ProviderSnapshotMaterialization],
    values: Mapping[str, ReconciliationValue],
    policy: ProviderPolicy,
) -> Result[ProviderSnapshotMaterialization]:
    trace = _build_reconciliation_trace(selected, materializations, values, policy)
    if isinstance(trace, Failure):
        return trace
    if isinstance(selected, Failure):
        if trace.value is None:
            return selected
        return _failure_with_trace(selected, trace.value)
    return _selected_materialization(selected.value, materializations, trace.value)


def _build_reconciliation_trace(
    selected: Result[FetchedProviderData[object]],
    materializations: Mapping[str, ProviderSnapshotMaterialization],
    values: Mapping[str, ReconciliationValue],
    policy: ProviderPolicy,
) -> Result[ReconciliationTrace | None]:
    trace_values = _dual_trace_values(materializations, values, policy)
    providers = tuple(
        route.provider for route in policy.routes if route.provider in trace_values
    )
    if len(providers) != 2:
        return Success(None)
    primary_name, secondary_name = providers
    primary = materializations.get(primary_name)
    secondary = materializations.get(secondary_name)
    if primary is None or secondary is None:
        return _failure(ErrorCode.CONFLICT, "Reconciliation candidates are unavailable")
    decision = reconcile_comparable_values(
        trace_values[primary_name], trace_values[secondary_name], policy
    )
    both_empty = _both_legitimate_empty(primary, secondary)
    outcome = _trace_outcome(decision.state, decision.reasons, both_empty=both_empty)
    selected_provider = (
        selected.value.provider if isinstance(selected, Success) else None
    )
    try:
        trace = ReconciliationTrace(
            policy_id=policy.policy_id,
            policy_threshold=policy.reconciliation_threshold,
            relative_difference=decision.relative_difference,
            decision=outcome,
            selected_provider=selected_provider,
            primary=_candidate_trace(primary, trace_values[primary_name]),
            secondary=_candidate_trace(secondary, trace_values[secondary_name]),
        )
    except (TypeError, ValueError, ValidationError):
        return _failure(ErrorCode.CONFLICT, "Reconciliation trace is invalid")
    return Success(trace)


def _dual_trace_values(
    materializations: Mapping[str, ProviderSnapshotMaterialization],
    values: Mapping[str, ReconciliationValue],
    policy: ProviderPolicy,
) -> Mapping[str, ReconciliationValue]:
    if len(values) == 2:
        return values
    empty_providers = tuple(
        route.provider
        for route in policy.routes
        if (
            (value := materializations.get(route.provider)) is not None
            and value.observation.state is ProviderDataState.LEGITIMATE_EMPTY
        )
    )
    if len(empty_providers) != 2:
        return {}
    return {
        provider: ReconciliationValue(metric="record_count", value=Decimal("0"))
        for provider in empty_providers
    }


def _both_legitimate_empty(
    primary: ProviderSnapshotMaterialization,
    secondary: ProviderSnapshotMaterialization,
) -> bool:
    return all(
        value.observation.state is ProviderDataState.LEGITIMATE_EMPTY
        for value in (primary, secondary)
    )


def _candidate_trace(
    materialization: ProviderSnapshotMaterialization,
    value: ReconciliationValue,
) -> ReconciliationCandidateTrace:
    return ReconciliationCandidateTrace(
        provider=materialization.provider,
        provider_version=materialization.provider_version,
        endpoint=materialization.endpoint,
        raw_content_hash=hashlib.sha256(materialization.raw_payload).hexdigest(),
        normalized_content_hash=normalized_evidence_content_hash(
            materialization.evidence
        ),
        metric=value.metric,
        value=value.value,
    )


def _trace_outcome(
    state: ProviderDataState,
    reasons: tuple[str, ...],
    *,
    both_empty: bool,
) -> ReconciliationOutcome:
    if both_empty:
        return ReconciliationOutcome.SELECTED_BOTH_EMPTY
    if state is not ProviderDataState.CONFLICT:
        return ReconciliationOutcome.SELECTED_WITHIN_THRESHOLD
    if reasons == ("reconciliation_metric_mismatch",):
        return ReconciliationOutcome.REJECTED_METRIC_MISMATCH
    return ReconciliationOutcome.REJECTED_THRESHOLD_EXCEEDED


def _failure_with_trace(
    failure: Failure,
    trace: ReconciliationTrace,
) -> Failure:
    payload = trace.model_dump(mode="json")
    details = dict(failure.error.details)
    details.update(
        {
            "reconciliation_trace": payload,
            "reconciliation_trace_hash": stable_payload_hash(payload),
        }
    )
    return Failure(
        StructuredError(
            code=failure.error.code,
            message=failure.error.message,
            details=details,
        )
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
