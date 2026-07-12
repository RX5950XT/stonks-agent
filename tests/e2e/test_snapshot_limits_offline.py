from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.market_data.replay_snapshot import (
    ReplaySnapshotMaterializationSource,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.application.data.materialize_snapshot import materialize_snapshot
from stonks_agent.domain.dataset_snapshot import MAX_NORMALIZED_ITEM_BYTES
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.provider_policy import load_provider_policies
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.ports.artifact_store import ArtifactManifest

MANIFEST = Path("tests/fixtures/market_data/manifest.yaml")
POLICIES = Path("config/providers/default.yaml")
AS_OF = datetime(2026, 3, 10, 22, tzinfo=UTC)


class CountingArtifactStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    def finalize(
        self,
        content: object,
        *,
        metadata: object,
        finalized_at: object,
    ) -> Success[ArtifactManifest] | Failure:
        self.writes += 1
        return super().finalize(
            content,
            metadata=metadata,
            finalized_at=finalized_at,
        )


def test_untrusted_replay_normalization_limit_fails_before_offline_archive() -> None:
    policy = next(
        item
        for item in load_provider_policies(POLICIES)
        if item.policy_id == "us-prices/1"
    )
    fetched = ReplaySnapshotMaterializationSource(MANIFEST, policy).fetch(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=AS_OF,
            query={"symbol": "AAPL", "interval": "1d", "scenario": "canonical"},
        ),
        provider_policy_id=policy.policy_id,
    )
    assert isinstance(fetched, Success)
    oversized = fetched.value.evidence[0].model_copy(
        update={"payload": {"value": "x" * (MAX_NORMALIZED_ITEM_BYTES + 1)}}
    )
    untrusted = fetched.value.model_copy(update={"evidence": (oversized,)})
    store = CountingArtifactStore()

    result = materialize_snapshot(
        CreateSnapshotRequest(
            market="US",
            capability="prices",
            as_of=AS_OF,
            query={"symbol": "AAPL", "interval": "1d", "scenario": "canonical"},
            provider_policy_id=policy.policy_id,
            idempotency_key="offline-limit-e2e",
            requested_at=AS_OF,
        ),
        untrusted,
        store,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert store.writes == 0
