from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.application.data.materialize_snapshot import (
    materialize_snapshot,
    verify_snapshot_artifacts,
)
from stonks_agent.application.data.policy_snapshot_source import (
    PolicySnapshotMaterializationSource,
)
from stonks_agent.domain.data_quality import (
    ProviderDataState,
    ProviderHealthState,
    ProviderObservation,
    ProviderRuntimeHealth,
)
from stonks_agent.domain.dataset_snapshot import (
    CanonicalDatasetSnapshotManifest,
    MaterializedEvidence,
    MaterializedSnapshot,
    ProviderSnapshotMaterialization,
    normalized_evidence_content_hash,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.provider_policy import (
    ProviderPolicy,
    ProviderRoute,
    ReconciliationOutcome,
    ReconciliationValue,
)
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)


class StaticSnapshotSource:
    def __init__(self, result: Result[ProviderSnapshotMaterialization]) -> None:
        self.result = result
        self.calls = 0

    def fetch(
        self,
        request: FetchDataRequest,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        self.calls += 1
        return self.result


class CloseReconciliation:
    def extract(
        self,
        provider: str,
        observation: ProviderObservation[object],
    ) -> ReconciliationValue | None:
        if not observation.data:
            return None
        payload = observation.data[-1]
        if not isinstance(payload, dict):
            return None
        close = payload.get("close")
        if not isinstance(close, str):
            return None
        return ReconciliationValue(metric="close", value=Decimal(close))


def route(
    provider: str,
    *,
    freshness_seconds: int = 0,
    quota_floor: int = 0,
) -> ProviderRoute:
    return ProviderRoute(
        provider=provider,
        origin=f"https://{provider}.example",
        endpoints=("/v1/prices",),
        freshness_seconds=freshness_seconds,
        quota_floor=quota_floor,
    )


def test_two_accepted_candidates_over_threshold_fail_closed() -> None:
    primary = StaticSnapshotSource(Success(materialization("primary", "100.00")))
    secondary = StaticSnapshotSource(Success(materialization("secondary", "103.00")))
    source = policy_source(primary=primary, secondary=secondary)

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert result.error.details["reason"] == "reconciliation_threshold_exceeded"
    trace = result.error.details["reconciliation_trace"]
    assert isinstance(trace, dict)
    assert trace["decision"] == "rejected_threshold_exceeded"
    assert trace["selected_provider"] is None
    assert trace["primary"]["provider"] == "primary"
    assert trace["secondary"]["provider"] == "secondary"
    assert trace["primary"]["provider_version"] == "fixture/1"
    assert trace["secondary"]["provider_version"] == "fixture/1"
    assert trace["primary"]["endpoint"] == "/v1/prices"
    assert trace["secondary"]["endpoint"] == "/v1/prices"
    assert trace["primary"]["value"] == "100.00"
    assert trace["secondary"]["value"] == "103.00"
    assert (
        trace["primary"]["raw_content_hash"]
        == hashlib.sha256(b'{"provider":"primary","close":"100.00"}').hexdigest()
    )
    assert (
        trace["secondary"]["raw_content_hash"]
        == hashlib.sha256(b'{"provider":"secondary","close":"103.00"}').hexdigest()
    )
    assert trace["policy_threshold"] == "0.01"
    assert trace["relative_difference"] == "0.02912621359223300970873786408"
    assert result.error.details["reconciliation_trace_hash"] == stable_payload_hash(
        trace
    )
    assert primary.calls == 1
    assert secondary.calls == 1


def test_two_candidates_within_threshold_keep_primary_raw_provenance() -> None:
    primary = StaticSnapshotSource(Success(materialization("primary", "100.00")))
    secondary = StaticSnapshotSource(Success(materialization("secondary", "100.50")))
    source = policy_source(primary=primary, secondary=secondary)

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Success)
    assert result.value.provider == "primary"
    assert result.value.raw_payload == b'{"provider":"primary","close":"100.00"}'
    trace = result.value.reconciliation_trace
    assert trace is not None
    assert trace.decision is ReconciliationOutcome.SELECTED_WITHIN_THRESHOLD
    assert trace.selected_provider == "primary"
    assert trace.policy_threshold == Decimal("0.01")
    assert trace.relative_difference == Decimal("0.004975124378109452736318407960")
    assert trace.primary.provider_version == "fixture/1"
    assert trace.primary.endpoint == "/v1/prices"
    assert (
        trace.primary.raw_content_hash
        == hashlib.sha256(b'{"provider":"primary","close":"100.00"}').hexdigest()
    )
    assert trace.primary.metric == trace.secondary.metric == "close"
    assert trace.primary.value == Decimal("100.00")
    assert trace.secondary.value == Decimal("100.50")
    assert primary.calls == 1
    assert secondary.calls == 1


def test_successful_reconciliation_trace_is_canonical_and_offline_verifiable() -> None:
    source = policy_source(
        primary=StaticSnapshotSource(Success(materialization("primary", "100.00"))),
        secondary=StaticSnapshotSource(Success(materialization("secondary", "100.50"))),
    )
    selected = source.fetch(request(), provider_policy_id="us-prices/1")
    assert isinstance(selected, Success)
    store = MemoryArtifactStore()

    snapshot = materialize_snapshot(snapshot_request(), selected.value, store)

    assert isinstance(snapshot, Success)
    trace = snapshot.value.manifest.reconciliation_trace
    assert trace == selected.value.reconciliation_trace
    assert trace is not None
    assert trace.primary.normalized_content_hash == normalized_evidence_content_hash(
        materialization("primary", "100.00").evidence
    )
    assert trace.secondary.normalized_content_hash == normalized_evidence_content_hash(
        materialization("secondary", "100.50").evidence
    )
    assert isinstance(verify_snapshot_artifacts(snapshot.value, store), Success)

    without_trace = materialize_snapshot(
        snapshot_request(),
        selected.value.model_copy(update={"reconciliation_trace": None}),
        MemoryArtifactStore(),
    )
    assert isinstance(without_trace, Success)
    assert without_trace.value.manifest_artifact_hash != (
        snapshot.value.manifest_artifact_hash
    )


def test_manifest_reconciliation_trace_tampering_fails_closed() -> None:
    selected = policy_source(
        primary=StaticSnapshotSource(Success(materialization("primary", "100.00"))),
        secondary=StaticSnapshotSource(Success(materialization("secondary", "100.50"))),
    ).fetch(request(), provider_policy_id="us-prices/1")
    assert isinstance(selected, Success)
    snapshot = materialize_snapshot(
        snapshot_request(), selected.value, MemoryArtifactStore()
    )
    assert isinstance(snapshot, Success)
    payload = snapshot.value.manifest.model_dump(mode="json")
    trace = dict(payload["reconciliation_trace"])
    secondary = dict(trace["secondary"])
    secondary["normalized_content_hash"] = "a" * 64
    trace["secondary"] = secondary
    payload["reconciliation_trace"] = trace

    tampered = CanonicalDatasetSnapshotManifest.model_validate(payload)
    assert tampered.content_hash != snapshot.value.manifest_artifact_hash
    snapshot_payload = snapshot.value.model_dump(mode="python")
    snapshot_payload["manifest"] = tampered

    with pytest.raises(ValidationError):
        MaterializedSnapshot.model_validate(snapshot_payload)


def test_reconciliation_uses_persisted_evidence_not_detached_observation() -> None:
    primary = StaticSnapshotSource(
        Success(
            materialization(
                "primary",
                "150.00",
                observation_close="100.00",
            )
        )
    )
    secondary = StaticSnapshotSource(Success(materialization("secondary", "100.50")))
    source = policy_source(primary=primary, secondary=secondary)

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Failure)
    assert result.error.details["reason"] == "reconciliation_threshold_exceeded"


def test_outage_fallback_returns_the_only_accepted_candidate() -> None:
    primary = StaticSnapshotSource(
        failure(ErrorCode.DATA_UNAVAILABLE, "primary unavailable")
    )
    secondary = StaticSnapshotSource(Success(materialization("secondary", "101.00")))
    source = policy_source(primary=primary, secondary=secondary)

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Success)
    assert result.value.provider == "secondary"
    assert result.value.raw_payload.startswith(b'{"provider":"secondary"')
    assert result.value.reconciliation_trace is None
    assert primary.calls == 1
    assert secondary.calls == 1


def test_two_legitimate_empty_candidates_archive_an_explicit_core_trace() -> None:
    primary = StaticSnapshotSource(Success(empty_materialization("primary")))
    secondary = StaticSnapshotSource(Success(empty_materialization("secondary")))
    source = policy_source(primary=primary, secondary=secondary)

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Success)
    trace = result.value.reconciliation_trace
    assert trace is not None
    assert trace.decision is ReconciliationOutcome.SELECTED_BOTH_EMPTY
    assert trace.selected_provider == "primary"
    assert trace.primary.metric == trace.secondary.metric == "record_count"
    assert trace.primary.value == trace.secondary.value == 0
    snapshot = materialize_snapshot(
        snapshot_request(), result.value, MemoryArtifactStore()
    )
    assert isinstance(snapshot, Success)
    assert snapshot.value.manifest.reconciliation_trace == trace


def test_single_provider_works_with_an_injected_strategy() -> None:
    primary = StaticSnapshotSource(Success(materialization("primary", "100.00")))
    source = PolicySnapshotMaterializationSource(
        policy=policy(routes=(route("primary"),)),
        sources={"primary": primary},
        reconciliation_strategy=CloseReconciliation(),
    )

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Success)
    assert result.value.provider == "primary"
    assert result.value.reconciliation_trace is None
    assert primary.calls == 1


def test_provider_cannot_supply_a_forged_core_reconciliation_trace() -> None:
    valid = policy_source(
        primary=StaticSnapshotSource(Success(materialization("primary", "100.00"))),
        secondary=StaticSnapshotSource(Success(materialization("secondary", "100.50"))),
    ).fetch(request(), provider_policy_id="us-prices/1")
    assert isinstance(valid, Success)
    trace = valid.value.reconciliation_trace
    assert trace is not None
    forged = materialization("primary", "100.00").model_copy(
        update={"reconciliation_trace": trace}
    )
    source = PolicySnapshotMaterializationSource(
        policy=policy(routes=(route("primary"),)),
        sources={"primary": StaticSnapshotSource(Success(forged))},
        reconciliation_strategy=CloseReconciliation(),
    )

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED


def test_unbounded_strategy_value_fails_without_echoing_unsafe_details() -> None:
    class UnboundedStrategy:
        def extract(
            self,
            provider: str,
            observation: ProviderObservation[object],
        ) -> ReconciliationValue | None:
            return ReconciliationValue.model_construct(
                metric="close",
                value=Decimal("1e1000"),
            )

    source = PolicySnapshotMaterializationSource(
        policy=policy(routes=(route("primary"), route("secondary"))),
        sources={
            "primary": StaticSnapshotSource(
                Success(materialization("primary", "100.00"))
            ),
            "secondary": StaticSnapshotSource(
                Success(materialization("secondary", "100.50"))
            ),
        },
        reconciliation_strategy=UnboundedStrategy(),
    )

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Failure)
    assert result.error.details["reason"] == "reconciliation_strategy_failed"
    assert "reconciliation_trace" not in result.error.details
    assert "1E+1000" not in str(result.error.details)


@pytest.mark.parametrize(
    ("configured_route", "health", "expected_state"),
    (
        (
            route("primary"),
            ProviderRuntimeHealth(
                state=ProviderHealthState.UNAVAILABLE,
                checked_at=NOW,
            ),
            ProviderDataState.PROVIDER_UNHEALTHY,
        ),
        (
            route("primary", freshness_seconds=10),
            ProviderRuntimeHealth(
                state=ProviderHealthState.HEALTHY,
                checked_at=NOW,
                latest_data_at=NOW - timedelta(seconds=11),
            ),
            ProviderDataState.STALE,
        ),
        (
            route("primary", quota_floor=5),
            ProviderRuntimeHealth(
                state=ProviderHealthState.HEALTHY,
                checked_at=NOW,
                remaining_quota=4,
            ),
            ProviderDataState.QUOTA_EXHAUSTED,
        ),
    ),
)
def test_runtime_health_freshness_and_quota_gate_before_fetch(
    configured_route: ProviderRoute,
    health: ProviderRuntimeHealth,
    expected_state: ProviderDataState,
) -> None:
    primary = StaticSnapshotSource(Success(materialization("primary", "100.00")))
    source = PolicySnapshotMaterializationSource(
        policy=policy(routes=(configured_route,)),
        sources={"primary": primary},
        runtime_health={"primary": health},
        reconciliation_strategy=CloseReconciliation(),
    )

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Failure)
    assert result.error.details["attempted_states"] == (
        ("primary", expected_state.value),
    )
    assert primary.calls == 0


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    (("rogue", "/v1/prices"), ("primary", "/rogue")),
)
def test_source_cannot_override_policy_route(provider: str, endpoint: str) -> None:
    primary = StaticSnapshotSource(
        Success(materialization(provider, "100.00", endpoint=endpoint))
    )
    source = PolicySnapshotMaterializationSource(
        policy=policy(routes=(route("primary"),)),
        sources={"primary": primary},
        reconciliation_strategy=CloseReconciliation(),
    )

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED


def test_route_violation_cannot_be_masked_by_a_healthy_fallback() -> None:
    primary = StaticSnapshotSource(Success(materialization("rogue", "100.00")))
    secondary = StaticSnapshotSource(Success(materialization("secondary", "100.00")))
    source = policy_source(primary=primary, secondary=secondary)

    result = source.fetch(request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
    assert primary.calls == 1
    assert secondary.calls == 1


def test_policy_identity_mismatch_stops_before_fetch() -> None:
    primary = StaticSnapshotSource(Success(materialization("primary", "100.00")))
    source = PolicySnapshotMaterializationSource(
        policy=policy(routes=(route("primary"),)),
        sources={"primary": primary},
        reconciliation_strategy=CloseReconciliation(),
    )

    result = source.fetch(request(), provider_policy_id="other-policy/1")

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
    assert primary.calls == 0


def policy_source(
    *,
    primary: StaticSnapshotSource,
    secondary: StaticSnapshotSource,
) -> PolicySnapshotMaterializationSource:
    return PolicySnapshotMaterializationSource(
        policy=policy(routes=(route("primary"), route("secondary"))),
        sources={"primary": primary, "secondary": secondary},
        reconciliation_strategy=CloseReconciliation(),
    )


def policy(*, routes: tuple[ProviderRoute, ...]) -> ProviderPolicy:
    return ProviderPolicy(
        policy_id="us-prices/1",
        market="US",
        capability="prices",
        routes=routes,
        reconciliation_threshold=Decimal("0.01"),
    )


def request() -> FetchDataRequest:
    return FetchDataRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
    )


def snapshot_request() -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
        provider_policy_id="us-prices/1",
        idempotency_key="reconciliation-trace-test",
        owner_subject="test-owner",
        requested_at=NOW,
    )


def materialization(
    provider: str,
    close: str,
    *,
    endpoint: str = "/v1/prices",
    observation_close: str | None = None,
) -> ProviderSnapshotMaterialization:
    timeline = EvidenceTimeline(
        event_time=NOW,
        published_at=NOW,
        available_at=NOW,
        observed_at=NOW,
        as_of=NOW,
        availability_certainty=AvailabilityCertainty.PROVEN,
    )
    return ProviderSnapshotMaterialization(
        provider=provider,
        provider_version="fixture/1",
        endpoint=endpoint,
        raw_payload=f'{{"provider":"{provider}","close":"{close}"}}'.encode(),
        raw_media_type="application/json",
        license_tag="CC0-1.0",
        redistribution_tag="synthetic-unrestricted",
        sensitivity=Sensitivity.PUBLIC,
        observation=ProviderObservation[object](
            state=ProviderDataState.AVAILABLE,
            data=({"close": observation_close or close},),
            completeness=Decimal("1"),
            observed_at=NOW,
        ),
        evidence=(
            MaterializedEvidence(
                subject="AAPL",
                kind="market_data",
                payload={"close": close},
                timeline=timeline,
            ),
        ),
    )


def empty_materialization(provider: str) -> ProviderSnapshotMaterialization:
    value = materialization(provider, "0")
    return ProviderSnapshotMaterialization.model_validate(
        value.model_copy(
            update={
                "observation": ProviderObservation[object](
                    state=ProviderDataState.LEGITIMATE_EMPTY,
                    data=(),
                    completeness=Decimal("1"),
                    observed_at=NOW,
                ),
                "evidence": (),
            }
        ).model_dump(mode="python")
    )


def failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
