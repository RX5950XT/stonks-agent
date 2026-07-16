from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.market_data.replay import (
    LoadedReplayFixture,
    ReplayDataset,
    ReplayMarketDataAdapter,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.application.data.materialize_snapshot import (
    materialize_snapshot,
    verify_snapshot_artifacts,
)
from stonks_agent.domain.data_quality import ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MaterializedEvidence,
    ProviderSnapshotMaterialization,
)
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_contracts.evidence import Sensitivity

FIXTURE_ROOT = Path("tests/fixtures/market_data")
MANIFEST = FIXTURE_ROOT / "manifest.yaml"


def unwrap[T](value: Success[T] | Failure) -> T:
    assert isinstance(value, Success)
    return value.value


def _canonical_fixture(
    adapter: ReplayMarketDataAdapter,
) -> LoadedReplayFixture:
    return next(
        item
        for item in adapter.fixtures
        if item.entry.fixture_id == "us-daily-dst-actions"
    )


def _request(
    fixture: LoadedReplayFixture, *, reversed_query: bool = False
) -> CreateSnapshotRequest:
    query = (
        {
            "scenario": fixture.entry.scenario,
            "interval": fixture.entry.interval,
            "symbol": fixture.entry.symbol,
        }
        if reversed_query
        else {
            "symbol": fixture.entry.symbol,
            "interval": fixture.entry.interval,
            "scenario": fixture.entry.scenario,
        }
    )
    return CreateSnapshotRequest(
        market=fixture.entry.market,
        capability=fixture.entry.capability,
        as_of=fixture.entry.as_of,
        query=query,
        provider_policy_id="us-prices/1",
        idempotency_key="offline-replay-two"
        if reversed_query
        else "offline-replay-one",
        owner_subject="test-owner",
        requested_at=(
            fixture.entry.observed_at + timedelta(days=1)
            if reversed_query
            else fixture.entry.observed_at
        ),
    )


def _evidence(dataset: ReplayDataset) -> tuple[MaterializedEvidence, ...]:
    bars = tuple(
        MaterializedEvidence(
            subject=dataset.symbol,
            kind="market_data",
            payload=bar.model_dump(mode="json"),
            timeline=bar.timeline,
        )
        for bar in dataset.bars
    )
    actions = tuple(
        MaterializedEvidence(
            subject=dataset.symbol,
            kind="market_data",
            payload=action.model_dump(mode="json"),
            timeline=action.timeline,
        )
        for action in dataset.corporate_actions
    )
    return bars + actions


def _materialization(
    fixture: LoadedReplayFixture,
    dataset: ReplayDataset,
    observation: ProviderObservation[ReplayDataset],
    *,
    reverse_evidence: bool = False,
) -> ProviderSnapshotMaterialization:
    items = _evidence(dataset)
    evidence = tuple(reversed(items)) if reverse_evidence else items
    canonical_observation = ProviderObservation[object](
        state=observation.state,
        data=tuple(item.payload for item in evidence),
        completeness=observation.completeness,
        reasons=observation.reasons,
        observed_at=observation.observed_at,
    )
    return ProviderSnapshotMaterialization(
        provider="replay",
        provider_version="fixture-manifest/1",
        endpoint="/v1/prices",
        raw_payload=(FIXTURE_ROOT / fixture.entry.path).read_bytes(),
        raw_media_type="application/json",
        license_tag=fixture.entry.license_tag,
        redistribution_tag=fixture.entry.redistribution_tag,
        sensitivity=Sensitivity.PUBLIC,
        observation=canonical_observation,
        evidence=evidence,
    )


def test_replay_adapter_materializes_hash_identical_offline_snapshot() -> None:
    first_adapter = ReplayMarketDataAdapter(MANIFEST)
    first_fixture = _canonical_fixture(first_adapter)
    first_request = _request(first_fixture)
    first_observation = first_adapter.fetch(
        FetchDataRequest(
            market=first_request.market,
            capability=first_request.capability,
            as_of=first_request.as_of,
            query=first_request.query,
        )
    )
    first_dataset = first_observation.data[0]
    first_store = MemoryArtifactStore()

    first = unwrap(
        materialize_snapshot(
            first_request,
            _materialization(first_fixture, first_dataset, first_observation),
            first_store,
        )
    )

    assert unwrap(verify_snapshot_artifacts(first, first_store)) is True
    raw = unwrap(first_store.read(first.raw_artifact_hash))
    assert raw == (FIXTURE_ROOT / first_fixture.entry.path).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == first.raw_artifact_hash
    assert len(first.manifest.entries) == 4
    assert all(
        entry.raw_artifact_hash == first.raw_artifact_hash
        for entry in first.manifest.entries
    )

    second_adapter = ReplayMarketDataAdapter(MANIFEST)
    second_fixture = _canonical_fixture(second_adapter)
    second_request = _request(second_fixture, reversed_query=True)
    second_observation = second_adapter.fetch(
        FetchDataRequest(
            market=second_request.market,
            capability=second_request.capability,
            as_of=second_request.as_of,
            query=second_request.query,
        )
    )
    second_store = MemoryArtifactStore()
    second = unwrap(
        materialize_snapshot(
            second_request,
            _materialization(
                second_fixture,
                second_observation.data[0],
                second_observation,
                reverse_evidence=True,
            ),
            second_store,
        )
    )

    assert second.snapshot_id == first.snapshot_id
    assert second.manifest_artifact_hash == first.manifest_artifact_hash
    assert second.manifest == first.manifest
    assert unwrap(second_store.read(second.manifest_artifact_hash)) == unwrap(
        first_store.read(first.manifest_artifact_hash)
    )
