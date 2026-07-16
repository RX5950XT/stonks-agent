from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.adapters.market_data.replay_snapshot import (
    ReplaySnapshotMaterializationSource,
)
from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.repositories import PostgresEvidenceRepository
from stonks_agent.adapters.postgres.snapshot_completion import (
    PostgresSnapshotCompletionStore,
)
from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.application.data.policy_snapshot_source import (
    PolicySnapshotMaterializationSource,
)
from stonks_agent.application.data.process_snapshot_lease import process_snapshot_lease
from stonks_agent.domain.auth import Role
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MaterializedEvidence,
    ProviderSnapshotMaterialization,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.provider_policy import (
    ProviderPolicy,
    ProviderRoute,
    ReconciliationValue,
)
from stonks_agent.entrypoints.api.routes.data import create_data_app
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_agent.ports.snapshot_materialization import SnapshotMaterializationSource
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.evidence import EvidenceKind, Sensitivity

pytestmark = pytest.mark.postgres
pytest_plugins = ("integration.postgres.conftest",)
TOKEN = "e2e-local-token-that-is-at-least-32-chars"
FIXTURE_MANIFEST = Path("tests/fixtures/market_data/manifest.yaml")
AS_OF = datetime(2026, 3, 10, 22, tzinfo=UTC)


class StaticSource:
    def __init__(self, result: Result[ProviderSnapshotMaterialization]) -> None:
        self.result = result
        self.calls = 0

    def fetch(
        self,
        request: FetchDataRequest,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        del request, provider_policy_id
        self.calls += 1
        return self.result


class CloseReconciliation:
    def extract(
        self,
        provider: str,
        observation: ProviderObservation[object],
    ) -> ReconciliationValue | None:
        del provider
        payload = observation.data[-1] if observation.data else None
        close = payload.get("close") if isinstance(payload, dict) else None
        if not isinstance(close, str):
            return None
        return ReconciliationValue(metric="close", value=Decimal(close))


class CountingArtifactStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.finalize_calls = 0

    def finalize(
        self,
        content: object,
        *,
        metadata: object,
        finalized_at: object,
    ) -> Result[ArtifactManifest]:
        self.finalize_calls += 1
        return super().finalize(
            content,
            metadata=metadata,
            finalized_at=finalized_at,
        )


def test_api_request_policy_replay_and_canonical_completion_vertical_slice(
    clean_database: Engine,
) -> None:
    pending = submit_snapshot(clean_database, "e2e-policy-replay")
    lease, claim_time = claim_snapshot(clean_database, "core-policy-worker")
    policy = replay_policy()
    artifacts = MemoryArtifactStore()

    completed = process_snapshot_lease(
        lease,
        now=claim_time + timedelta(seconds=1),
        source=policy_source(
            policy,
            {"replay": ReplaySnapshotMaterializationSource(FIXTURE_MANIFEST, policy)},
        ),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=policy,
    )

    receipt = unwrap(completed)
    assert str(lease.job_id) == pending["job_id"]
    assert len(receipt.evidence_refs) == 4
    assert artifacts.is_finalized(receipt.result_artifact_hash)
    assert canonical_counts(clean_database) == (2, 4, 1, 4, 1)
    assert persisted_evidence_providers(clean_database, receipt.evidence_refs) == {
        "replay"
    }


def test_primary_outage_falls_back_to_replay_and_commits_canonical_snapshot(
    clean_database: Engine,
) -> None:
    submit_snapshot(clean_database, "e2e-outage-fallback")
    lease, claim_time = claim_snapshot(clean_database, "core-fallback-worker")
    policy = fallback_policy()
    primary = StaticSource(provider_outage())

    completed = process_snapshot_lease(
        lease,
        now=claim_time + timedelta(seconds=1),
        source=policy_source(
            policy,
            {
                "primary": primary,
                "replay": ReplaySnapshotMaterializationSource(FIXTURE_MANIFEST, policy),
            },
        ),
        artifacts=MemoryArtifactStore(),
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=policy,
    )

    receipt = unwrap(completed)
    assert primary.calls == 1
    assert canonical_counts(clean_database) == (2, 4, 1, 4, 1)
    assert persisted_evidence_providers(clean_database, receipt.evidence_refs) == {
        "replay"
    }


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    (("rogue", "/v1/prices"), ("primary", "/rogue")),
)
def test_policy_rejects_rogue_route_before_canonical_writes_and_audits_failure(
    clean_database: Engine,
    provider: str,
    endpoint: str,
) -> None:
    submit_snapshot(clean_database, f"e2e-rogue-{provider}-{endpoint}")
    lease, claim_time = claim_snapshot(clean_database, "core-authority-worker")
    policy = primary_policy()
    artifacts = CountingArtifactStore()

    result = process_snapshot_lease(
        lease,
        now=claim_time + timedelta(seconds=1),
        source=policy_source(
            policy,
            {"primary": StaticSource(Success(materialization(provider, endpoint)))},
        ),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=policy,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CAPABILITY_DENIED
    assert artifacts.finalize_calls == 0
    assert canonical_counts(clean_database) == (0, 0, 0, 0, 0)
    assert failure_audit(clean_database) == expected_failure_audit("capability_denied")


def test_reconciliation_conflict_has_zero_canonical_writes_and_fenced_audit(
    clean_database: Engine,
) -> None:
    submit_snapshot(clean_database, "e2e-reconciliation-conflict")
    lease, claim_time = claim_snapshot(clean_database, "core-reconcile-worker")
    policy = conflict_policy()
    artifacts = CountingArtifactStore()

    result = process_snapshot_lease(
        lease,
        now=claim_time + timedelta(seconds=1),
        source=policy_source(
            policy,
            {
                "primary": StaticSource(
                    Success(materialization("primary", close="100"))
                ),
                "secondary": StaticSource(
                    Success(materialization("secondary", close="103"))
                ),
            },
        ),
        artifacts=artifacts,
        completions=PostgresSnapshotCompletionStore(clean_database),
        policy=policy,
    )

    assert isinstance(result, Failure)
    assert result.error.details["reason"] == "reconciliation_threshold_exceeded"
    assert artifacts.finalize_calls == 0
    assert canonical_counts(clean_database) == (0, 0, 0, 0, 0)
    audit = failure_audit(clean_database)
    assert audit[:5] == expected_failure_audit("data_unavailable")[:5]
    event_payload, outbox_payload = failure_payloads(clean_database)
    assert event_payload == outbox_payload
    trace = event_payload["reconciliation_trace"]
    trace_hash = stable_payload_hash(trace)
    assert event_payload["reconciliation_trace_hash"] == trace_hash
    assert trace["decision"] == "rejected_threshold_exceeded"
    assert trace["primary"]["value"] == "100"
    assert trace["secondary"]["value"] == "103"
    assert audit[5] == {
        "code": "data_unavailable",
        "stage": "provider",
        "attempt_generation": 1,
        "reconciliation_trace_hash": trace_hash,
    }


def submit_snapshot(engine: Engine, idempotency_key: str) -> dict[str, object]:
    with engine.connect() as connection:
        requested_at = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(requested_at, datetime)
    client = TestClient(
        create_data_app(
            PostgresSnapshotRequestStore(engine),
            LocalTokenAuthenticator(
                environment="test",
                token=TOKEN,
                subject="e2e-researcher",
                roles=frozenset({Role.RESEARCHER}),
                allowed_hosts=frozenset({"testclient"}),
            ),
            clock=lambda: requested_at,
        )
    )
    response = client.post(
        "/v1/data/snapshots",
        headers={"authorization": f"Bearer {TOKEN}"},
        json={
            "market": "US",
            "capability": "prices",
            "as_of": AS_OF.isoformat(),
            "query": {"symbol": "AAPL", "interval": "1d", "scenario": "canonical"},
            "provider_policy_id": "us-prices/1",
            "idempotency_key": idempotency_key,
        },
    )
    assert response.status_code == 202
    pending = response.json()["data"]
    assert pending["snapshot_id"] is None
    return pending


def claim_snapshot(engine: Engine, worker_id: str) -> tuple[JobLease, datetime]:
    claim_time = datetime.now(UTC) + timedelta(seconds=1)
    lease = unwrap(
        PostgresJobQueue(engine).claim(
            worker_id=worker_id,
            now=claim_time,
            lease_for=timedelta(minutes=5),
        )
    )
    return lease, claim_time


def policy_source(
    policy: ProviderPolicy,
    sources: dict[str, SnapshotMaterializationSource[FetchDataRequest]],
) -> PolicySnapshotMaterializationSource:
    return PolicySnapshotMaterializationSource(
        policy=policy,
        sources=sources,
        reconciliation_strategy=CloseReconciliation(),
    )


def replay_policy() -> ProviderPolicy:
    return policy((route("replay"),))


def primary_policy() -> ProviderPolicy:
    return policy((route("primary"),))


def fallback_policy() -> ProviderPolicy:
    return policy((route("primary"), route("replay")))


def conflict_policy() -> ProviderPolicy:
    return policy((route("primary"), route("secondary")))


def policy(routes: tuple[ProviderRoute, ...]) -> ProviderPolicy:
    return ProviderPolicy(
        policy_id="us-prices/1",
        market="US",
        capability="prices",
        routes=routes,
        reconciliation_threshold=Decimal("0.01"),
    )


def route(provider: str) -> ProviderRoute:
    return ProviderRoute(
        provider=provider,
        origin=f"https://{provider}.example",
        endpoints=("/v1/prices",),
        freshness_seconds=0,
        quota_floor=0,
    )


def provider_outage() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="Primary provider unavailable",
        )
    )


def materialization(
    provider: str,
    endpoint: str = "/v1/prices",
    *,
    close: str = "100",
) -> ProviderSnapshotMaterialization:
    timeline = EvidenceTimeline(
        event_time=AS_OF - timedelta(minutes=1),
        published_at=AS_OF - timedelta(seconds=30),
        available_at=AS_OF,
        observed_at=AS_OF,
        as_of=AS_OF,
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
            data=({"close": close},),
            completeness=Decimal("1"),
            observed_at=AS_OF,
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


def canonical_counts(engine: Engine) -> tuple[int, ...]:
    tables = (
        "artifact_manifest",
        "evidence_item",
        "dataset_snapshot",
        "dataset_snapshot_evidence",
        "run_dataset_snapshot",
    )
    with engine.connect() as connection:
        return tuple(
            int(connection.scalar(text(f"select count(*) from {table}")) or 0)
            for table in tables
        )


def failure_audit(engine: Engine) -> tuple[object, ...]:
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                select j.status, j.lease_owner, r.status, e.event_type, o.topic,
                       j.last_error
                from job j
                join run r using (run_id)
                join run_event e using (run_id)
                join outbox o on o.aggregate_id = r.run_id::text
                """
            )
        ).one()


def failure_payloads(engine: Engine) -> tuple[dict[str, object], dict[str, object]]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                select e.payload, o.payload
                from run_event e
                join outbox o on o.aggregate_id = e.run_id::text
                """
            )
        ).one()
    return row[0], row[1]


def expected_failure_audit(error_code: str) -> tuple[object, ...]:
    return (
        "queued",
        None,
        "running",
        "snapshot.retry_scheduled",
        "snapshot.retry_scheduled",
        {"code": error_code, "stage": "provider", "attempt_generation": 1},
    )


def persisted_evidence_providers(
    engine: Engine,
    evidence_refs: tuple[object, ...],
) -> set[str]:
    with Session(engine) as session:
        repository = PostgresEvidenceRepository(session)
        evidence = tuple(unwrap(repository.get(item)) for item in evidence_refs)
    assert {item.kind for item in evidence} == {EvidenceKind.MARKET_DATA}
    assert all(item.raw_artifact_ref.startswith("sha256:") for item in evidence)
    return {item.provider for item in evidence}


def unwrap[T](result: Result[T]) -> T:
    assert isinstance(result, Success)
    return result.value
