from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from stonks_agent.adapters.artifacts.local import LocalArtifactStore
from stonks_agent.adapters.paper_cycle.object_resolver import (
    ArtifactPaperCycleObjectResolver,
)
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.paper_cycle import CanonicalCycleReference
from stonks_contracts.common import canonical_json, stable_payload_hash
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 7, 28, 6, tzinfo=UTC)
OBJECT_ID = UUID("73100000-0000-4000-8000-000000000001")


class StoredObject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object_id: UUID
    value: str

    @property
    def semantic_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


def test_resolver_loads_exact_typed_artifact_and_revalidates_reference(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    value = StoredObject(object_id=OBJECT_ID, value="durable")
    manifest = store.finalize(
        canonical_json(value.model_dump(mode="json")).encode(),
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="Apache-2.0",
            sensitivity=Sensitivity.INTERNAL,
            source="test-paper-cycle",
        ),
        finalized_at=NOW,
    )
    assert isinstance(manifest, Success)
    reference = CanonicalCycleReference(
        ref_type="stored_object",
        ref_id=str(value.object_id),
        content_hash=manifest.value.content_hash,
    )

    result = ArtifactPaperCycleObjectResolver(store).resolve(
        reference,
        object_type=StoredObject,
        object_id=lambda item: str(item.object_id),
        semantic_hash=lambda item: item.semantic_hash,
    )

    assert result == Success(value)


def test_resolver_rejects_wrong_id_semantic_hash_or_schema(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    value = StoredObject(object_id=OBJECT_ID, value="durable")
    manifest = store.finalize(
        canonical_json(value.model_dump(mode="json")).encode(),
        metadata=ArtifactMetadata(
            media_type="application/json",
            license_tag="Apache-2.0",
            sensitivity=Sensitivity.INTERNAL,
            source="test-paper-cycle",
        ),
        finalized_at=NOW,
    )
    assert isinstance(manifest, Success)
    resolver = ArtifactPaperCycleObjectResolver(store)
    reference = CanonicalCycleReference(
        ref_type="stored_object",
        ref_id=str(UUID("73100000-0000-4000-8000-000000000002")),
        content_hash=manifest.value.content_hash,
    )

    wrong_id = resolver.resolve(
        reference,
        object_type=StoredObject,
        object_id=lambda item: str(item.object_id),
        semantic_hash=lambda item: item.semantic_hash,
    )
    wrong_hash = resolver.resolve(
        reference.model_copy(update={"ref_id": str(OBJECT_ID)}),
        object_type=StoredObject,
        object_id=lambda item: str(item.object_id),
        semantic_hash=lambda _: "f" * 64,
    )
    wrong_schema = resolver.resolve(
        reference.model_copy(update={"ref_id": str(OBJECT_ID)}),
        object_type=StoredObject,
        object_id=lambda item: str(item.object_id),
        semantic_hash=lambda item: item.semantic_hash,
    )
    object_path = (
        tmp_path
        / "artifacts"
        / "objects"
        / manifest.value.content_hash[:2]
        / manifest.value.content_hash
    )
    object_path.write_bytes(b'{"unexpected":true}')
    corrupt = resolver.resolve(
        reference.model_copy(update={"ref_id": str(OBJECT_ID)}),
        object_type=StoredObject,
        object_id=lambda item: str(item.object_id),
        semantic_hash=lambda item: item.semantic_hash,
    )

    assert isinstance(wrong_id, Failure)
    assert wrong_id.error.code is ErrorCode.CONFLICT
    assert isinstance(wrong_hash, Failure)
    assert wrong_hash.error.code is ErrorCode.CONFLICT
    assert isinstance(wrong_schema, Success)
    assert isinstance(corrupt, Failure)
    assert corrupt.error.code is ErrorCode.CONFLICT
