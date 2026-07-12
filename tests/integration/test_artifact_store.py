from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Protocol

import pytest

from stonks_agent.adapters.artifacts.local import LocalArtifactStore
from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.ports.artifact_store import ArtifactManifest, ArtifactStore
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
CONTENT = b'{"symbol":"AAPL","close":"100.00"}'


class StoreFactory(Protocol):
    def __call__(self, tmp_path: Path) -> ArtifactStore: ...


@pytest.fixture(params=["memory", "local"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ArtifactStore:
    if request.param == "memory":
        return MemoryArtifactStore(max_size_bytes=1024)
    return LocalArtifactStore(tmp_path / "artifacts", max_size_bytes=1024)


def metadata(*, license_tag: str = "test-only") -> ArtifactMetadata:
    return ArtifactMetadata(
        media_type="application/json",
        license_tag=license_tag,
        sensitivity=Sensitivity.INTERNAL,
        source="fixture",
    )


def unwrap[T](result: Result[T]) -> T:
    assert isinstance(result, Success)
    return result.value


def test_finalize_is_content_addressed_and_verified(store: ArtifactStore) -> None:
    manifest = unwrap(store.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))

    assert manifest.content_hash == hashlib.sha256(CONTENT).hexdigest()
    assert manifest.size_bytes == len(CONTENT)
    assert manifest.storage_uri == f"artifact://sha256/{manifest.content_hash}"
    assert unwrap(store.read(manifest.content_hash)) == CONTENT
    assert unwrap(store.manifest(manifest.content_hash)) == manifest
    assert store.is_finalized(manifest.content_hash)


def test_retry_returns_original_manifest_instead_of_mutating_time(
    store: ArtifactStore,
) -> None:
    first = unwrap(store.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))
    retried = unwrap(
        store.finalize(
            CONTENT,
            metadata=metadata(),
            finalized_at=NOW + timedelta(days=1),
        )
    )

    assert retried == first
    assert retried.finalized_at == NOW


def test_same_content_with_conflicting_metadata_fails_closed(
    store: ArtifactStore,
) -> None:
    unwrap(store.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))

    result = store.finalize(
        CONTENT,
        metadata=metadata(license_tag="different-license"),
        finalized_at=NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_invalid_content_and_oversize_are_structured_failures(
    store: ArtifactStore,
) -> None:
    invalid = store.finalize("not-bytes", metadata=metadata(), finalized_at=NOW)
    oversize = store.finalize(b"x" * 1025, metadata=metadata(), finalized_at=NOW)

    assert isinstance(invalid, Failure)
    assert invalid.error.code is ErrorCode.INVALID_INPUT
    assert isinstance(oversize, Failure)
    assert oversize.error.code is ErrorCode.INVALID_INPUT


def test_concurrent_finalize_has_one_immutable_manifest(store: ArtifactStore) -> None:
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(
            executor.map(
                lambda _: store.finalize(
                    CONTENT,
                    metadata=metadata(),
                    finalized_at=NOW,
                ),
                range(8),
            )
        )

    manifests = tuple(unwrap(result) for result in results)
    assert len(set(manifests)) == 1


def test_distinct_local_store_instances_cannot_overwrite_manifest_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    stores = (
        LocalArtifactStore(root, max_size_bytes=5 * 1024 * 1024),
        LocalArtifactStore(root, max_size_bytes=5 * 1024 * 1024),
    )
    payload = b"x" * (4 * 1024 * 1024)
    start = Barrier(2)

    def finalize(index: int) -> Result[ArtifactManifest]:
        start.wait()
        return stores[index].finalize(
            payload,
            metadata=metadata(license_tag=f"license-{index}"),
            finalized_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(finalize, range(2)))

    successes = tuple(result for result in results if isinstance(result, Success))
    conflicts = tuple(result for result in results if isinstance(result, Failure))
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].error.code is ErrorCode.CONFLICT
    stored = unwrap(stores[0].manifest(successes[0].value.content_hash))
    assert stored == successes[0].value


def test_local_store_detects_object_corruption(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)
    manifest = unwrap(store.finalize(CONTENT, metadata=metadata(), finalized_at=NOW))
    object_path = root / "objects" / manifest.content_hash[:2] / manifest.content_hash
    object_path.write_bytes(b"corrupted")

    result = store.read(manifest.content_hash)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_unknown_or_invalid_hash_is_not_path_traversal(store: ArtifactStore) -> None:
    unknown = store.read("a" * 64)
    invalid = store.read("../../secrets")

    assert isinstance(unknown, Failure)
    assert unknown.error.code is ErrorCode.NOT_FOUND
    assert isinstance(invalid, Failure)
    assert invalid.error.code is ErrorCode.INVALID_INPUT


def test_manifest_rejects_unknown_fields() -> None:
    payload = {
        "content_hash": "a" * 64,
        "size_bytes": 1,
        "metadata": metadata().model_dump(mode="json"),
        "finalized_at": NOW,
        "storage_uri": f"artifact://sha256/{'a' * 64}",
        "mutable": True,
    }

    with pytest.raises(ValueError):
        ArtifactManifest.model_validate(payload)
