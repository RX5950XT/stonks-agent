from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.data.materialize_snapshot import materialize_snapshot
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MAX_EVIDENCE_ITEMS,
    MAX_JSON_DEPTH,
    MAX_JSON_KEY_LENGTH,
    MAX_JSON_LIST_ITEMS,
    MAX_JSON_MAP_ITEMS,
    MAX_NORMALIZED_ITEM_BYTES,
    MAX_NORMALIZED_TOTAL_BYTES,
    MAX_RAW_PAYLOAD_BYTES,
    MAX_REASON_LENGTH,
    MAX_REASONS,
    MaterializedEvidence,
    ProviderSnapshotMaterialization,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.ports.artifact_store import ArtifactManifest
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 3, 10, 22, tzinfo=UTC)


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


def _request() -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL", "interval": "1d"},
        provider_policy_id="us-prices/1",
        idempotency_key="snapshot-limit-test",
        requested_at=NOW,
    )


def _timeline() -> EvidenceTimeline:
    return EvidenceTimeline(
        event_time=NOW - timedelta(minutes=2),
        published_at=NOW - timedelta(minutes=1),
        available_at=NOW,
        observed_at=NOW,
        as_of=NOW,
        availability_certainty=AvailabilityCertainty.PROVEN,
        strict_point_in_time=True,
    )


def _evidence(payload: dict[str, object] | None = None) -> MaterializedEvidence:
    return MaterializedEvidence(
        subject="AAPL",
        kind="market_data",
        payload=payload or {"close": "100.00"},
        timeline=_timeline(),
    )


def _materialization() -> ProviderSnapshotMaterialization:
    return ProviderSnapshotMaterialization(
        provider="replay",
        provider_version="fixture-manifest/1",
        endpoint="/v1/prices",
        raw_payload=b'{"close":"100.00"}',
        raw_media_type="application/json",
        license_tag="CC0-1.0",
        redistribution_tag="synthetic-unrestricted",
        sensitivity=Sensitivity.PUBLIC,
        observation=ProviderObservation[object](
            state=ProviderDataState.AVAILABLE,
            data=({"close": "100.00"},),
            completeness=Decimal("1"),
            observed_at=NOW,
        ),
        evidence=(_evidence(),),
    )


def _assert_rejected_without_write(value: ProviderSnapshotMaterialization) -> None:
    store = CountingArtifactStore()

    result = materialize_snapshot(_request(), value, store)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert store.writes == 0


def test_oversized_raw_payload_is_rejected_before_artifact_write() -> None:
    _assert_rejected_without_write(
        _materialization().model_copy(
            update={"raw_payload": b"x" * (MAX_RAW_PAYLOAD_BYTES + 1)}
        )
    )


def test_domain_boundary_rejects_oversized_raw_payload() -> None:
    payload = _materialization().model_dump(mode="python")
    payload["raw_payload"] = b"x" * (MAX_RAW_PAYLOAD_BYTES + 1)

    with pytest.raises(ValidationError, match="at most"):
        ProviderSnapshotMaterialization.model_validate(payload)


def test_oversized_evidence_count_is_rejected_before_hash_or_sort() -> None:
    item = _evidence()
    _assert_rejected_without_write(
        _materialization().model_copy(
            update={"evidence": (item,) * (MAX_EVIDENCE_ITEMS + 1)}
        )
    )


def test_domain_boundary_rejects_oversized_evidence_count() -> None:
    payload = _materialization().model_dump(mode="python")
    item = _evidence().model_dump(mode="python")
    payload["evidence"] = (item,) * (MAX_EVIDENCE_ITEMS + 1)

    with pytest.raises(ValidationError, match="at most"):
        ProviderSnapshotMaterialization.model_validate(payload)


def test_oversized_normalized_item_is_rejected_before_artifact_write() -> None:
    item = _evidence().model_copy(
        update={"payload": {"value": "x" * (MAX_NORMALIZED_ITEM_BYTES + 1)}}
    )
    _assert_rejected_without_write(
        _materialization().model_copy(update={"evidence": (item,)})
    )


def test_domain_boundary_rejects_oversized_normalized_item() -> None:
    with pytest.raises(ValidationError, match="item size limit"):
        _evidence({"value": "x" * (MAX_NORMALIZED_ITEM_BYTES + 1)})


def test_oversized_normalized_total_is_rejected_before_artifact_write() -> None:
    item = _evidence({"value": "x" * (MAX_NORMALIZED_ITEM_BYTES // 2)})
    count = (MAX_NORMALIZED_TOTAL_BYTES // (MAX_NORMALIZED_ITEM_BYTES // 2)) + 2
    _assert_rejected_without_write(
        _materialization().model_copy(update={"evidence": (item,) * count})
    )


def _too_deep_payload() -> dict[str, object]:
    root: dict[str, object] = {}
    current = root
    for _ in range(MAX_JSON_DEPTH + 1):
        child: dict[str, object] = {}
        current["child"] = child
        current = child
    return root


@pytest.mark.parametrize(
    "payload",
    [
        _too_deep_payload(),
        {"items": [0] * (MAX_JSON_LIST_ITEMS + 1)},
        {"mapping": {str(index): 0 for index in range(MAX_JSON_MAP_ITEMS + 1)}},
        {"x" * (MAX_JSON_KEY_LENGTH + 1): 0},
    ],
    ids=["depth", "list-count", "map-count", "key-length"],
)
def test_invalid_normalized_json_shape_is_rejected_before_artifact_write(
    payload: dict[str, object],
) -> None:
    item = _evidence().model_copy(update={"payload": payload})
    _assert_rejected_without_write(
        _materialization().model_copy(update={"evidence": (item,)})
    )


@pytest.mark.parametrize(
    "reasons",
    [
        ("reason",) * (MAX_REASONS + 1),
        ("x" * (MAX_REASON_LENGTH + 1),),
    ],
    ids=["count", "length"],
)
def test_oversized_reasons_are_rejected_before_artifact_write(
    reasons: tuple[str, ...],
) -> None:
    observation = _materialization().observation.model_copy(update={"reasons": reasons})
    _assert_rejected_without_write(
        _materialization().model_copy(update={"observation": observation})
    )


def test_domain_boundary_rejects_oversized_reasons() -> None:
    payload = _materialization().model_dump(mode="python")
    payload["observation"]["reasons"] = ["reason"] * (MAX_REASONS + 1)

    with pytest.raises(ValidationError, match="reason count"):
        ProviderSnapshotMaterialization.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "x" * (MAX_NORMALIZED_ITEM_BYTES + 1)},
        _too_deep_payload(),
        {"items": [0] * (MAX_JSON_LIST_ITEMS + 1)},
        {"mapping": {str(index): 0 for index in range(MAX_JSON_MAP_ITEMS + 1)}},
        {"x" * (MAX_JSON_KEY_LENGTH + 1): 0},
    ],
    ids=["size", "depth", "list-count", "map-count", "key-length"],
)
def test_invalid_observation_json_is_rejected_before_artifact_write(
    payload: dict[str, object],
) -> None:
    observation = _materialization().observation.model_copy(update={"data": (payload,)})

    _assert_rejected_without_write(
        _materialization().model_copy(update={"observation": observation})
    )


def test_oversized_observation_count_is_rejected_before_artifact_write() -> None:
    observation = _materialization().observation.model_copy(
        update={"data": ({"close": "100.00"},) * (MAX_EVIDENCE_ITEMS + 1)}
    )

    _assert_rejected_without_write(
        _materialization().model_copy(update={"observation": observation})
    )


def test_oversized_observation_total_is_rejected_before_artifact_write() -> None:
    item = {"value": "x" * (MAX_NORMALIZED_ITEM_BYTES // 2)}
    count = (MAX_NORMALIZED_TOTAL_BYTES // (MAX_NORMALIZED_ITEM_BYTES // 2)) + 2
    observation = _materialization().observation.model_copy(
        update={"data": (item,) * count}
    )

    _assert_rejected_without_write(
        _materialization().model_copy(update={"observation": observation})
    )


def test_observation_must_reference_exact_normalized_payloads() -> None:
    observation = _materialization().observation.model_copy(
        update={"data": ({"close": "999.00"},)}
    )

    _assert_rejected_without_write(
        _materialization().model_copy(update={"observation": observation})
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "ROGUE"),
        ("provider_version", "x" * 129),
        ("endpoint", "https://evil.example/v1/prices"),
        ("raw_media_type", "not-a-media-type"),
        ("license_tag", ""),
        ("redistribution_tag", "x" * 129),
        ("sensitivity", "unknown"),
    ],
)
def test_model_copy_cannot_bypass_outer_contract_before_artifact_write(
    field: str,
    value: object,
) -> None:
    _assert_rejected_without_write(_materialization().model_copy(update={field: value}))


def test_model_copy_cannot_bypass_observation_state_contract() -> None:
    observation = _materialization().observation.model_copy(update={"reasons": ("",)})

    _assert_rejected_without_write(
        _materialization().model_copy(update={"observation": observation})
    )
