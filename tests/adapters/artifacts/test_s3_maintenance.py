from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from stonks_agent.adapters.artifacts.s3_maintenance import (
    S3ArtifactMaintenanceBackend,
)
from stonks_agent.domain.artifact_retention import (
    ArtifactEncryption,
    ArtifactGCDisposition,
    ArtifactGCRequest,
    ArtifactRestoreRequest,
    ArtifactRetentionMode,
    ArtifactRetentionRequest,
    EnableArtifactLegalHold,
)
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.ports.artifact_store import ArtifactManifest

NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
HASH = "a" * 64
LOCKED_HASH = "b" * 64
NEW_HASH = "c" * 64
UNKNOWN_HASH = "d" * 64
FINAL_HASH = "e" * 64
BUCKET = "stonks-artifacts"
PREFIX = "prod/artifacts"
OWNER = "123456789012"
OPERATION = UUID("82000000-0000-4000-8000-000000000001")


class Store:
    def __init__(self, finalized: set[str]) -> None:
        self.finalized = finalized

    def finalize(self, content: object, *, metadata: object, finalized_at: object):
        raise AssertionError("maintenance cannot finalize bytes")

    def read(self, content_hash: str):
        if content_hash in self.finalized:
            return Success(content_hash.encode())
        return Failure(
            StructuredError(code=ErrorCode.NOT_FOUND, message="Artifact not found")
        )

    def manifest(self, content_hash: str):
        if content_hash in self.finalized:
            return Success(ArtifactManifest.model_construct(content_hash=content_hash))
        return Failure(
            StructuredError(code=ErrorCode.NOT_FOUND, message="Artifact not found")
        )

    def is_finalized(self, content_hash: str) -> bool:
        return content_hash in self.finalized


class Client:
    def __init__(self) -> None:
        self.versions: list[dict[str, object]] = []
        self.markers: list[dict[str, object]] = []
        self.retentions: dict[tuple[str, str], dict[str, object]] = {}
        self.holds: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_list_prefix: str | None = None
        self.fail_delete_version: str | None = None

    def list_object_versions(self, **kwargs: object) -> object:
        self.calls.append(("list", dict(kwargs)))
        prefix = str(kwargs["Prefix"])
        if prefix == self.fail_list_prefix:
            raise RuntimeError("provider details")
        return {
            "IsTruncated": False,
            "Versions": [
                value for value in self.versions if str(value["Key"]).startswith(prefix)
            ],
            "DeleteMarkers": [
                value for value in self.markers if str(value["Key"]).startswith(prefix)
            ],
        }

    def get_object_retention(self, **kwargs: object) -> object:
        self.calls.append(("get_retention", dict(kwargs)))
        key = str(kwargs["Key"]), str(kwargs["VersionId"])
        return {"Retention": self.retentions.get(key, {})}

    def put_object_retention(self, **kwargs: object) -> object:
        self.calls.append(("put_retention", dict(kwargs)))
        key = str(kwargs["Key"]), str(kwargs["VersionId"])
        self.retentions[key] = dict(kwargs["Retention"])  # type: ignore[arg-type]
        return {}

    def get_object_legal_hold(self, **kwargs: object) -> object:
        self.calls.append(("get_hold", dict(kwargs)))
        key = str(kwargs["Key"]), str(kwargs["VersionId"])
        return {"LegalHold": {"Status": self.holds.get(key, "OFF")}}

    def put_object_legal_hold(self, **kwargs: object) -> object:
        self.calls.append(("put_hold", dict(kwargs)))
        key = str(kwargs["Key"]), str(kwargs["VersionId"])
        self.holds[key] = str(dict(kwargs["LegalHold"])["Status"])  # type: ignore[arg-type]
        return {}

    def delete_object(self, **kwargs: object) -> object:
        self.calls.append(("delete", dict(kwargs)))
        version_id = str(kwargs["VersionId"])
        if version_id == self.fail_delete_version:
            raise RuntimeError("provider details")
        self.versions = [
            value for value in self.versions if value["VersionId"] != version_id
        ]
        self.markers = [
            value for value in self.markers if value["VersionId"] != version_id
        ]
        return {}


class PaginatedClient(Client):
    def list_object_versions(self, **kwargs: object) -> object:
        prefix = str(kwargs["Prefix"])
        self.calls.append(("list", dict(kwargs)))
        root = f"{PREFIX}/objects/"
        if prefix != root:
            return {"IsTruncated": False, "Versions": [], "DeleteMarkers": []}
        if "KeyMarker" not in kwargs:
            return {
                "IsTruncated": True,
                "Versions": [self.versions[0]],
                "DeleteMarkers": [],
                "NextKeyMarker": str(self.versions[0]["Key"]),
                "NextVersionIdMarker": str(self.versions[0]["VersionId"]),
            }
        return {
            "IsTruncated": False,
            "Versions": [self.versions[1]],
            "DeleteMarkers": [],
        }


def object_key(content_hash: str) -> str:
    return f"{PREFIX}/objects/{content_hash[:2]}/{content_hash}"


def manifest_key(content_hash: str) -> str:
    return f"{PREFIX}/manifests/{content_hash[:2]}/{content_hash}.json"


def version(key: str, version_id: str, modified: datetime, *, latest: bool = True):
    return {
        "Key": key,
        "VersionId": version_id,
        "LastModified": modified,
        "IsLatest": latest,
    }


def add_finalized(client: Client, content_hash: str = HASH) -> None:
    client.versions.extend(
        (
            version(object_key(content_hash), f"object-{content_hash[0]}", NOW),
            version(manifest_key(content_hash), f"manifest-{content_hash[0]}", NOW),
        )
    )


def backend(
    client: Client,
    finalized: set[str],
    *,
    clock: Callable[[], datetime] | None = None,
) -> S3ArtifactMaintenanceBackend:
    return S3ArtifactMaintenanceBackend(
        store=Store(finalized),
        client=client,
        bucket=BUCKET,
        prefix=PREFIX,
        expected_bucket_owner=OWNER,
        encryption=ArtifactEncryption.KMS,
        clock=clock or (lambda: NOW),
    )


def retention_request(
    *,
    until: datetime = NOW + timedelta(days=30),
    mode: ArtifactRetentionMode = ArtifactRetentionMode.COMPLIANCE,
) -> ArtifactRetentionRequest:
    return ArtifactRetentionRequest(
        operation_id=OPERATION,
        content_hash=HASH,
        retain_until=until,
        mode=mode,
        actor="operator:retention",
        reason="regulatory_archive",
        requested_at=NOW,
    )


def test_retention_extends_both_exact_versions_without_bypass() -> None:
    client = Client()
    add_finalized(client)
    for item in client.versions:
        client.retentions[(str(item["Key"]), str(item["VersionId"]))] = {
            "Mode": "GOVERNANCE",
            "RetainUntilDate": NOW + timedelta(days=1),
        }

    result = backend(client, {HASH}).extend_retention(retention_request())

    assert isinstance(result, Success)
    assert result.value.retention_mode is ArtifactRetentionMode.COMPLIANCE
    assert result.value.retain_until == NOW + timedelta(days=30)
    writes = [
        payload for operation, payload in client.calls if operation == "put_retention"
    ]
    assert {payload["VersionId"] for payload in writes} == {"object-a", "manifest-a"}
    assert all("BypassGovernanceRetention" not in payload for payload in writes)


def test_retention_refuses_shortening_or_compliance_downgrade_before_write() -> None:
    client = Client()
    add_finalized(client)
    for item in client.versions:
        client.retentions[(str(item["Key"]), str(item["VersionId"]))] = {
            "Mode": "COMPLIANCE",
            "RetainUntilDate": NOW + timedelta(days=60),
        }

    result = backend(client, {HASH}).extend_retention(
        retention_request(
            until=NOW + timedelta(days=30),
            mode=ArtifactRetentionMode.GOVERNANCE,
        )
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert not any(operation == "put_retention" for operation, _ in client.calls)


def test_legal_hold_turns_on_and_verifies_both_exact_versions() -> None:
    client = Client()
    add_finalized(client)
    for item in client.versions:
        client.retentions[(str(item["Key"]), str(item["VersionId"]))] = {
            "Mode": "GOVERNANCE",
            "RetainUntilDate": NOW + timedelta(days=30),
        }

    result = backend(client, {HASH}).enable_legal_hold(
        EnableArtifactLegalHold(
            operation_id=OPERATION,
            content_hash=HASH,
            actor="operator:legal",
            reason="litigation_hold",
            requested_at=NOW,
        )
    )

    assert isinstance(result, Success)
    assert result.value.legal_hold is True
    assert set(client.holds.values()) == {"ON"}
    assert len(client.holds) == 2


def test_gc_only_deletes_old_unfinalized_unlocked_exact_versions() -> None:
    client = Client()
    old = NOW - timedelta(days=30)
    recent = NOW - timedelta(hours=1)
    for content_hash, modified in (
        (HASH, old),
        (LOCKED_HASH, old),
        (NEW_HASH, recent),
        (UNKNOWN_HASH, old),
        (FINAL_HASH, old),
    ):
        item = version(object_key(content_hash), f"v-{content_hash[0]}", modified)
        client.versions.append(item)
        client.holds[(str(item["Key"]), str(item["VersionId"]))] = "OFF"
    client.versions.append(
        version(manifest_key(FINAL_HASH), "manifest-e", old, latest=False)
    )
    client.retentions[(object_key(LOCKED_HASH), "v-b")] = {
        "Mode": "COMPLIANCE",
        "RetainUntilDate": NOW + timedelta(days=30),
    }
    client.fail_list_prefix = manifest_key(UNKNOWN_HASH)

    result = backend(client, {FINAL_HASH}).collect_orphans(
        ArtifactGCRequest(
            operation_id=OPERATION,
            cutoff_at=NOW - timedelta(days=7),
            max_candidates=100,
            actor="system:artifact-gc",
            reason="orphan_cleanup",
            requested_at=NOW,
        )
    )

    assert isinstance(result, Success)
    dispositions = {item.content_hash: item.disposition for item in result.value.items}
    assert dispositions == {
        HASH: ArtifactGCDisposition.DELETED,
        LOCKED_HASH: ArtifactGCDisposition.RETAINED_LOCKED,
        NEW_HASH: ArtifactGCDisposition.RETAINED_TOO_NEW,
        UNKNOWN_HASH: ArtifactGCDisposition.RETAINED_UNKNOWN,
        FINAL_HASH: ArtifactGCDisposition.RETAINED_FINALIZED,
    }
    deletes = [payload for operation, payload in client.calls if operation == "delete"]
    assert deletes == [
        {
            "Bucket": BUCKET,
            "Key": object_key(HASH),
            "VersionId": "v-a",
            "ExpectedBucketOwner": OWNER,
        }
    ]


def test_gc_delete_failure_preserves_unknown_and_never_uses_bypass() -> None:
    client = Client()
    client.versions.append(version(object_key(HASH), "v-a", NOW - timedelta(days=30)))
    client.fail_delete_version = "v-a"

    result = backend(client, set()).collect_orphans(
        ArtifactGCRequest(
            operation_id=OPERATION,
            cutoff_at=NOW - timedelta(days=7),
            max_candidates=100,
            actor="system:artifact-gc",
            reason="orphan_cleanup",
            requested_at=NOW,
        )
    )

    assert isinstance(result, Success)
    assert result.value.items[0].disposition is ArtifactGCDisposition.RETAINED_UNKNOWN
    assert all(
        "BypassGovernanceRetention" not in payload
        for operation, payload in client.calls
        if operation == "delete"
    )


def test_gc_uses_bounded_version_pagination_without_marker_reuse() -> None:
    client = PaginatedClient()
    client.versions.extend(
        (
            version(object_key(HASH), "v-a", NOW - timedelta(days=30)),
            version(object_key(LOCKED_HASH), "v-b", NOW - timedelta(days=30)),
        )
    )

    result = backend(client, set()).collect_orphans(
        ArtifactGCRequest(
            operation_id=OPERATION,
            cutoff_at=NOW - timedelta(days=7),
            max_candidates=2,
            actor="system:artifact-gc",
            reason="orphan_cleanup",
            requested_at=NOW,
        )
    )

    assert isinstance(result, Success)
    assert result.value.scanned == 2
    assert {item.disposition for item in result.value.items} == {
        ArtifactGCDisposition.DELETED
    }
    pages = [
        payload
        for operation, payload in client.calls
        if operation == "list" and payload["Prefix"] == f"{PREFIX}/objects/"
    ]
    assert len(pages) == 2
    assert pages[1]["KeyMarker"] == object_key(HASH)
    assert pages[1]["VersionIdMarker"] == "v-a"


def test_restore_removes_only_exact_latest_delete_markers_then_verifies() -> None:
    client = Client()
    add_finalized(client)
    client.markers.extend(
        (
            version(object_key(HASH), "delete-object", NOW, latest=True),
            version(manifest_key(HASH), "delete-manifest", NOW, latest=True),
            version(
                object_key(HASH), "old-marker", NOW - timedelta(days=1), latest=False
            ),
        )
    )
    for item in client.versions:
        item["IsLatest"] = False

    result = backend(client, {HASH}).restore(
        ArtifactRestoreRequest(
            operation_id=OPERATION,
            content_hash=HASH,
            actor="operator:restore",
            reason="delete_marker_recovery",
            requested_at=NOW,
        )
    )

    assert isinstance(result, Success)
    assert result.value.removed_delete_markers == 2
    deleted = {
        payload["VersionId"]
        for operation, payload in client.calls
        if operation == "delete"
    }
    assert deleted == {"delete-object", "delete-manifest"}
    assert any(marker["VersionId"] == "old-marker" for marker in client.markers)


def test_invalid_or_rolling_back_clock_denies_before_storage_access() -> None:
    request = retention_request()
    for clock in (
        lambda: datetime(2026, 7, 18, 12),
        lambda: NOW - timedelta(seconds=1),
        lambda: (_ for _ in ()).throw(RuntimeError("clock details")),
    ):
        client = Client()

        result = backend(client, {HASH}, clock=clock).extend_retention(request)

        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.INTERNAL_ERROR
        assert client.calls == []
