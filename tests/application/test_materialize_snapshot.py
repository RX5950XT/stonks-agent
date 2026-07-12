from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.data.materialize_snapshot import (
    materialize_snapshot,
    verify_snapshot_artifacts,
)
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    CanonicalDatasetSnapshotManifest,
    MaterializedEvidence,
    MaterializedSnapshot,
    ProviderSnapshotMaterialization,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.ports.artifact_store import ArtifactManifest, ArtifactStore
from stonks_contracts.evidence import Sensitivity

AS_OF = datetime(2026, 1, 2, 21, tzinfo=UTC)
RAW = b'{"prices":[{"close":"100.00","symbol":"AAPL"}]}'


class RecordingArtifactStore:
    def __init__(self) -> None:
        self.delegate = MemoryArtifactStore()
        self.finalized: list[bytes] = []

    def finalize(
        self,
        content: object,
        *,
        metadata: object,
        finalized_at: object,
    ) -> Success[ArtifactManifest] | Failure:
        if isinstance(content, bytes):
            self.finalized.append(content)
        return self.delegate.finalize(
            content,
            metadata=metadata,
            finalized_at=finalized_at,
        )

    def read(self, content_hash: str) -> Success[bytes] | Failure:
        return self.delegate.read(content_hash)

    def manifest(self, content_hash: str) -> Success[ArtifactManifest] | Failure:
        return self.delegate.manifest(content_hash)

    def is_finalized(self, content_hash: str) -> bool:
        return self.delegate.is_finalized(content_hash)


def request(
    *,
    query: dict[str, object] | None = None,
    idempotency_key: str = "snapshot-one",
    requested_at: datetime = AS_OF,
) -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=AS_OF,
        query=query or {"symbol": "AAPL", "interval": "1d"},
        provider_policy_id="us-prices/1",
        idempotency_key=idempotency_key,
        requested_at=requested_at,
    )


def timeline(
    *,
    event_offset: int = 2,
    available_at: datetime | None = None,
    certainty: AvailabilityCertainty = AvailabilityCertainty.PROVEN,
    strict: bool = True,
) -> EvidenceTimeline:
    return EvidenceTimeline(
        event_time=AS_OF - timedelta(minutes=event_offset),
        published_at=AS_OF - timedelta(minutes=1),
        available_at=available_at or AS_OF,
        observed_at=max(available_at or AS_OF, AS_OF),
        as_of=AS_OF,
        availability_certainty=certainty,
        strict_point_in_time=strict,
    )


def evidence(
    *,
    payload: dict[str, object] | None = None,
    event_offset: int = 2,
    evidence_timeline: EvidenceTimeline | None = None,
) -> MaterializedEvidence:
    return MaterializedEvidence(
        subject="AAPL",
        kind="market_data",
        payload=payload or {"symbol": "AAPL", "close": "100.00"},
        timeline=evidence_timeline or timeline(event_offset=event_offset),
    )


def materialization(
    *,
    state: ProviderDataState = ProviderDataState.AVAILABLE,
    evidence_items: tuple[MaterializedEvidence, ...] | None = None,
) -> ProviderSnapshotMaterialization:
    items = evidence_items if evidence_items is not None else (evidence(),)
    if state is ProviderDataState.LEGITIMATE_EMPTY:
        observation_data: tuple[object, ...] = ()
        completeness = Decimal("1")
        reasons: tuple[str, ...] = ()
    elif state in {
        ProviderDataState.AVAILABLE,
        ProviderDataState.STALE,
        ProviderDataState.PARTIAL,
    }:
        observation_data = tuple(item.payload for item in items)
        completeness = (
            Decimal("0.5") if state is ProviderDataState.PARTIAL else Decimal("1")
        )
        reasons = () if state is ProviderDataState.AVAILABLE else (state.value,)
    else:
        observation_data = ()
        completeness = Decimal("0")
        reasons = (state.value,)
    return ProviderSnapshotMaterialization(
        provider="replay",
        provider_version="fixture-manifest/1",
        endpoint="/v1/prices",
        raw_payload=RAW,
        raw_media_type="application/json",
        license_tag="CC0-1.0",
        redistribution_tag="synthetic-unrestricted",
        sensitivity=Sensitivity.PUBLIC,
        observation=ProviderObservation[object](
            state=state,
            data=observation_data,
            completeness=completeness,
            reasons=reasons,
            observed_at=AS_OF,
        ),
        evidence=items,
    )


def unwrap[T](value: Success[T] | Failure) -> T:
    assert isinstance(value, Success)
    return value.value


def test_manifest_identity_ignores_dict_order_request_time_and_idempotency() -> None:
    first_store = RecordingArtifactStore()
    second_store = RecordingArtifactStore()
    first_materialization = materialization(
        evidence_items=(
            evidence(payload={"symbol": "AAPL", "close": "100.00"}),
            evidence(
                payload={"volume": "10", "symbol": "AAPL"},
                event_offset=3,
            ),
        )
    )
    second_materialization = materialization(
        evidence_items=(
            evidence(
                payload={"symbol": "AAPL", "volume": "10"},
                event_offset=3,
            ),
            evidence(payload={"close": "100.00", "symbol": "AAPL"}),
        )
    )

    first = unwrap(
        materialize_snapshot(
            request(
                query={"symbol": "AAPL", "interval": "1d"},
                idempotency_key="caller-one",
                requested_at=AS_OF,
            ),
            first_materialization,
            first_store,
        )
    )
    second = unwrap(
        materialize_snapshot(
            request(
                query={"interval": "1d", "symbol": "AAPL"},
                idempotency_key="caller-two",
                requested_at=AS_OF + timedelta(days=1),
            ),
            second_materialization,
            second_store,
        )
    )

    assert first.snapshot_id == second.snapshot_id
    assert first.manifest_artifact_hash == second.manifest_artifact_hash
    assert first.manifest == second.manifest
    assert first.evidence_refs == second.evidence_refs
    assert first_store.finalized[0] == RAW
    assert second_store.finalized[0] == RAW
    assert first_store.finalized[1] == second_store.finalized[1]


def test_manifest_entries_retain_provenance_time_and_license_and_verify_hashes() -> (
    None
):
    store = RecordingArtifactStore()

    snapshot = unwrap(materialize_snapshot(request(), materialization(), store))
    entry = snapshot.manifest.entries[0]

    assert entry.provider == "replay"
    assert entry.provider_version == "fixture-manifest/1"
    assert entry.endpoint == "/v1/prices"
    assert entry.raw_artifact_hash == hashlib.sha256(RAW).hexdigest()
    assert entry.timeline == timeline()
    assert entry.license_tag == "CC0-1.0"
    assert entry.redistribution_tag == "synthetic-unrestricted"
    assert (
        entry.payload_hash
        == hashlib.sha256(b'{"close":"100.00","symbol":"AAPL"}').hexdigest()
    )
    assert snapshot.evidence_refs == (entry.evidence_id,)
    assert unwrap(store.read(snapshot.raw_artifact_hash)) == RAW
    assert unwrap(verify_snapshot_artifacts(snapshot, store)) is True


@pytest.mark.parametrize(
    "bad_timeline",
    [
        timeline(
            available_at=AS_OF + timedelta(seconds=1),
            certainty=AvailabilityCertainty.PROVEN,
            strict=False,
        ),
        timeline(certainty=AvailabilityCertainty.UNKNOWN, strict=False),
    ],
)
def test_future_or_unknown_point_in_time_evidence_fails_before_raw_archive(
    bad_timeline: EvidenceTimeline,
) -> None:
    store = RecordingArtifactStore()

    result = materialize_snapshot(
        request(),
        materialization(evidence_items=(evidence(evidence_timeline=bad_timeline),)),
        store,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert store.finalized == []
    assert not store.is_finalized(hashlib.sha256(RAW).hexdigest())


@pytest.mark.parametrize(
    "state",
    [
        ProviderDataState.NOT_SUPPORTED,
        ProviderDataState.CONFIG_MISSING,
        ProviderDataState.HEALTH_UNKNOWN,
        ProviderDataState.PROVIDER_UNHEALTHY,
        ProviderDataState.FRESHNESS_UNKNOWN,
        ProviderDataState.QUOTA_UNKNOWN,
        ProviderDataState.QUOTA_EXHAUSTED,
        ProviderDataState.CONFLICT,
        ProviderDataState.FETCH_FAILED,
    ],
)
def test_failed_provider_observation_never_becomes_empty_snapshot(
    state: ProviderDataState,
) -> None:
    store = RecordingArtifactStore()

    result = materialize_snapshot(
        request(),
        materialization(state=state, evidence_items=()),
        store,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
    assert store.finalized == []


def test_only_explicit_legitimate_empty_can_create_an_empty_snapshot() -> None:
    legitimate = unwrap(
        materialize_snapshot(
            request(),
            materialization(
                state=ProviderDataState.LEGITIMATE_EMPTY,
                evidence_items=(),
            ),
            MemoryArtifactStore(),
        )
    )
    confused = materialize_snapshot(
        request(),
        materialization().model_copy(update={"evidence": ()}),
        MemoryArtifactStore(),
    )

    assert legitimate.manifest.provider_state is ProviderDataState.LEGITIMATE_EMPTY
    assert legitimate.manifest.entries == ()
    assert isinstance(confused, Failure)
    assert confused.error.code is ErrorCode.INVALID_INPUT


def test_legitimate_empty_with_materialized_evidence_fails_closed() -> None:
    result = materialize_snapshot(
        request(),
        materialization(
            state=ProviderDataState.LEGITIMATE_EMPTY,
            evidence_items=(evidence(),),
        ),
        MemoryArtifactStore(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("state", [ProviderDataState.STALE, ProviderDataState.PARTIAL])
def test_policy_accepted_degraded_state_remains_explicit_in_manifest(
    state: ProviderDataState,
) -> None:
    snapshot = unwrap(
        materialize_snapshot(
            request(),
            materialization(state=state),
            MemoryArtifactStore(),
        )
    )

    assert snapshot.manifest.provider_state is state
    assert snapshot.manifest.reasons == (state.value,)


def test_duplicate_materialized_evidence_is_rejected() -> None:
    duplicate = evidence()

    result = materialize_snapshot(
        request(),
        materialization(evidence_items=(duplicate, duplicate)),
        MemoryArtifactStore(),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_non_json_materialized_payload_fails_closed_before_raw_archive() -> None:
    store = RecordingArtifactStore()
    untrusted_evidence = evidence().model_copy(update={"payload": {"bad": b"bytes"}})
    untrusted_materialization = materialization().model_copy(
        update={"evidence": (untrusted_evidence,)}
    )

    result = materialize_snapshot(
        request(),
        untrusted_materialization,
        store,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert store.finalized == []


def test_verifier_fails_when_artifacts_are_not_in_the_selected_store() -> None:
    snapshot = unwrap(
        materialize_snapshot(request(), materialization(), MemoryArtifactStore())
    )

    result = verify_snapshot_artifacts(snapshot, MemoryArtifactStore())

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.NOT_FOUND


def test_raw_or_manifest_artifact_finalize_failure_is_structured() -> None:
    raw_failure = materialize_snapshot(
        request(),
        materialization(),
        MemoryArtifactStore(max_size_bytes=1),
    )
    manifest_failure = materialize_snapshot(
        request(),
        materialization(),
        MemoryArtifactStore(max_size_bytes=len(RAW)),
    )

    assert isinstance(raw_failure, Failure)
    assert raw_failure.error.code is ErrorCode.INVALID_INPUT
    assert isinstance(manifest_failure, Failure)
    assert manifest_failure.error.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "case",
    [
        "request_hash",
        "failed_state",
        "empty_available",
        "incomplete_available",
        "invalid_partial",
        "degraded_without_reason",
        "duplicate_entry",
        "raw_mismatch",
        "provider_mismatch",
        "payload_hash",
        "unsafe_timeline",
    ],
)
def test_manifest_domain_invariants_reject_tampering(case: str) -> None:
    snapshot = unwrap(
        materialize_snapshot(request(), materialization(), MemoryArtifactStore())
    )
    payload = snapshot.manifest.model_dump(mode="json")
    entries = list(payload["entries"])
    if case == "request_hash":
        payload["request_hash"] = "a" * 64
    elif case == "failed_state":
        payload["provider_state"] = ProviderDataState.FETCH_FAILED.value
    elif case == "empty_available":
        payload["entries"] = []
    elif case == "incomplete_available":
        payload["completeness"] = "0.5"
    elif case == "invalid_partial":
        payload["provider_state"] = ProviderDataState.PARTIAL.value
    elif case == "degraded_without_reason":
        payload["provider_state"] = ProviderDataState.STALE.value
        payload["reasons"] = []
    elif case == "duplicate_entry":
        payload["entries"] = entries * 2
    elif case == "raw_mismatch":
        payload["raw_artifact_hash"] = "a" * 64
    elif case == "provider_mismatch":
        payload["provider"] = "other"
    elif case == "payload_hash":
        entry = dict(entries[0])
        entry["payload_hash"] = "a" * 64
        payload["entries"] = [entry]
    else:
        entry = dict(entries[0])
        entry["timeline"] = dict(entry["timeline"])
        entry["timeline"]["strict_point_in_time"] = False
        entry["timeline"]["availability_certainty"] = "unknown"
        payload["entries"] = [entry]

    with pytest.raises(ValidationError):
        CanonicalDatasetSnapshotManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_id", UUID("80000000-0000-4000-8000-000000000001")),
        ("manifest_artifact_hash", "a" * 64),
        ("raw_artifact_hash", "b" * 64),
        ("evidence_refs", ()),
    ],
)
def test_snapshot_reference_domain_invariants_reject_tampering(
    field: str,
    value: object,
) -> None:
    snapshot = unwrap(
        materialize_snapshot(request(), materialization(), MemoryArtifactStore())
    )
    payload = snapshot.model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        MaterializedSnapshot.model_validate(payload)


def assert_artifact_store(value: ArtifactStore) -> None:
    assert value is not None


assert_artifact_store(RecordingArtifactStore())
