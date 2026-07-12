"""Deterministic canonical dataset snapshot manifests."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBytes,
    field_validator,
    model_validator,
)

from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.evidence import AvailabilityCertainty, EvidenceTimeline
from stonks_agent.domain.provider_policy import ReconciliationOutcome
from stonks_contracts.common import (
    DecimalString,
    NonEmptyString,
    Sha256,
    UnitDecimal,
    UTCDateTime,
    canonical_json,
    stable_payload_hash,
)
from stonks_contracts.evidence import EvidenceKind, Sensitivity

MAX_RAW_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_ITEMS = 10_000
MAX_NORMALIZED_ITEM_BYTES = 512 * 1024
MAX_NORMALIZED_TOTAL_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_LIST_ITEMS = 10_000
MAX_JSON_MAP_ITEMS = 2_000
MAX_JSON_KEY_LENGTH = 256
MAX_REASONS = 32
MAX_REASON_LENGTH = 512
MAX_TRACE_TEXT_LENGTH = 256
MAX_TRACE_DECIMAL_DIGITS = 128


class ReconciliationCandidateTrace(BaseModel):
    """Bounded content references and scalar used for one provider candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    provider_version: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=MAX_TRACE_TEXT_LENGTH)
    raw_content_hash: Sha256
    normalized_content_hash: Sha256
    metric: str = Field(min_length=1, max_length=128)
    value: DecimalString

    @field_validator("provider_version", "endpoint", "metric")
    @classmethod
    def validate_safe_text(cls, value: str) -> str:
        if value.strip() != value or _contains_control(value):
            raise ValueError("reconciliation trace text is unsafe")
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "://" in value
            or "?" in value
            or "#" in value
        ):
            raise ValueError("reconciliation endpoint must be a relative path")
        return value

    @field_validator("value")
    @classmethod
    def validate_bounded_value(cls, value: Decimal) -> Decimal:
        _validate_bounded_decimal(value)
        return value


class ReconciliationTrace(BaseModel):
    """Core-derived dual-source decision included in canonical manifests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1.0.0", pattern=r"^1\.0\.0$")
    policy_id: str = Field(min_length=1, max_length=128)
    policy_threshold: UnitDecimal
    relative_difference: UnitDecimal
    decision: ReconciliationOutcome
    selected_provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    primary: ReconciliationCandidateTrace
    secondary: ReconciliationCandidateTrace

    @field_validator("policy_id")
    @classmethod
    def validate_policy_id(cls, value: str) -> str:
        if value.strip() != value or _contains_control(value):
            raise ValueError("reconciliation policy ID is unsafe")
        return value

    @field_validator("policy_threshold", "relative_difference")
    @classmethod
    def validate_bounded_ratio(cls, value: Decimal) -> Decimal:
        _validate_bounded_decimal(value)
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.primary.provider == self.secondary.provider:
            raise ValueError("reconciliation providers must be distinct")
        providers = {self.primary.provider, self.secondary.provider}
        selected = self.decision in {
            ReconciliationOutcome.SELECTED_WITHIN_THRESHOLD,
            ReconciliationOutcome.SELECTED_BOTH_EMPTY,
        }
        valid_selection = (
            self.selected_provider in providers
            if selected
            else self.selected_provider is None
        )
        if not valid_selection:
            raise ValueError("reconciliation selection is inconsistent")
        _validate_trace_decision(self)
        return self


class MaterializedEvidence(BaseModel):
    """One normalized datum derived from an archived provider response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: NonEmptyString
    kind: EvidenceKind
    payload: dict[str, object]
    timeline: EvidenceTimeline

    @model_validator(mode="after")
    def validate_normalized_payload(self) -> Self:
        normalized_payload_size(self.payload)
        return self


class ProviderSnapshotMaterialization(BaseModel):
    """Accepted provider output plus the raw and normalized representations."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    provider: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    provider_version: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=256)
    raw_payload: StrictBytes = Field(max_length=MAX_RAW_PAYLOAD_BYTES)
    raw_media_type: str = Field(
        min_length=3,
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
    )
    license_tag: str = Field(min_length=1, max_length=128)
    redistribution_tag: str = Field(min_length=1, max_length=128)
    sensitivity: Sensitivity
    observation: ProviderObservation[object]
    evidence: tuple[MaterializedEvidence, ...] = Field(
        default=(),
        max_length=MAX_EVIDENCE_ITEMS,
    )
    reconciliation_trace: ReconciliationTrace | None = None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "://" in value:
            raise ValueError("provider endpoint must be a relative path")
        return value

    @model_validator(mode="after")
    def validate_resource_limits(self) -> Self:
        validate_materialization_limits(self)
        _validate_materialization_reconciliation(self)
        return self


class SnapshotManifestEntry(BaseModel):
    """Immutable provenance reference for one canonical datum."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: UUID
    subject: NonEmptyString
    kind: EvidenceKind
    payload: dict[str, object]
    payload_hash: Sha256
    provider: NonEmptyString
    provider_version: NonEmptyString
    endpoint: NonEmptyString
    raw_artifact_hash: Sha256
    timeline: EvidenceTimeline
    license_tag: NonEmptyString
    redistribution_tag: NonEmptyString
    sensitivity: Sensitivity

    @model_validator(mode="after")
    def validate_payload_hash(self) -> Self:
        normalized_payload_size(self.payload)
        if stable_payload_hash(self.payload) != self.payload_hash:
            raise ValueError("snapshot entry payload hash is invalid")
        return self


class CanonicalDatasetSnapshotManifest(BaseModel):
    """Canonical JSON payload whose hash is the snapshot identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1.0.0", pattern=r"^1\.0\.0$")
    request_hash: Sha256
    market: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    as_of: UTCDateTime
    query: dict[str, object]
    provider_policy_id: NonEmptyString
    provider: NonEmptyString
    provider_version: NonEmptyString
    endpoint: NonEmptyString
    provider_state: ProviderDataState
    completeness: UnitDecimal
    reasons: tuple[str, ...] = Field(default=(), max_length=MAX_REASONS)
    provider_observed_at: UTCDateTime
    raw_artifact_hash: Sha256
    raw_media_type: NonEmptyString
    license_tag: NonEmptyString
    redistribution_tag: NonEmptyString
    sensitivity: Sensitivity
    reconciliation_trace: ReconciliationTrace | None = None
    entries: tuple[SnapshotManifestEntry, ...] = Field(max_length=MAX_EVIDENCE_ITEMS)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        validate_provider_reasons(self.reasons)
        _validate_normalized_total(self.entries)
        _validate_manifest_request(self)
        _validate_manifest_state(self)
        _validate_manifest_entries(self)
        _validate_manifest_reconciliation(self)
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self).encode("utf-8")

    @property
    def content_hash(self) -> str:
        return stable_payload_hash(self)

    @property
    def snapshot_id(self) -> UUID:
        return uuid5(NAMESPACE_URL, f"stonks:dataset-snapshot:{self.content_hash}")


class MaterializedSnapshot(BaseModel):
    """Reference-only result after raw and manifest artifacts are finalized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    manifest_artifact_hash: Sha256
    raw_artifact_hash: Sha256
    evidence_refs: tuple[UUID, ...]
    manifest: CanonicalDatasetSnapshotManifest

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if self.snapshot_id != self.manifest.snapshot_id:
            raise ValueError("snapshot ID must derive from the canonical manifest")
        if self.manifest_artifact_hash != self.manifest.content_hash:
            raise ValueError(
                "manifest artifact hash must match canonical manifest bytes"
            )
        if self.raw_artifact_hash != self.manifest.raw_artifact_hash:
            raise ValueError("snapshot raw artifact reference is inconsistent")
        expected_refs = tuple(item.evidence_id for item in self.manifest.entries)
        if self.evidence_refs != expected_refs:
            raise ValueError("snapshot evidence references must match manifest entries")
        return self


def normalized_evidence_content_hash(
    entries: tuple[MaterializedEvidence | SnapshotManifestEntry, ...],
) -> str:
    """Hash provider-independent normalized evidence for offline comparison."""

    projections = [
        {
            "subject": item.subject,
            "kind": item.kind,
            "payload_hash": (
                item.payload_hash
                if isinstance(item, SnapshotManifestEntry)
                else stable_payload_hash(item.payload)
            ),
            "timeline": item.timeline.model_dump(mode="json"),
        }
        for item in entries
    ]
    return stable_payload_hash(sorted(projections, key=stable_payload_hash))


def _validate_trace_decision(trace: ReconciliationTrace) -> None:
    primary = trace.primary
    secondary = trace.secondary
    if trace.decision is ReconciliationOutcome.SELECTED_BOTH_EMPTY:
        valid_empty = (
            primary.metric == secondary.metric == "record_count"
            and primary.value == secondary.value == 0
            and trace.relative_difference == 0
        )
        if not valid_empty:
            raise ValueError("empty reconciliation trace is inconsistent")
        return
    if primary.metric != secondary.metric:
        if (
            trace.decision is not ReconciliationOutcome.REJECTED_METRIC_MISMATCH
            or trace.relative_difference != 1
        ):
            raise ValueError("metric-mismatch reconciliation trace is inconsistent")
        return
    difference = _relative_difference(primary.value, secondary.value)
    if trace.relative_difference != difference:
        raise ValueError("reconciliation relative difference is inconsistent")
    if trace.decision is ReconciliationOutcome.SELECTED_WITHIN_THRESHOLD:
        valid = difference <= trace.policy_threshold
    else:
        valid = (
            trace.decision is ReconciliationOutcome.REJECTED_THRESHOLD_EXCEEDED
            and difference > trace.policy_threshold
        )
    if not valid:
        raise ValueError("reconciliation threshold decision is inconsistent")


def _relative_difference(primary: Decimal, secondary: Decimal) -> Decimal:
    denominator = max(abs(primary), abs(secondary), Decimal("0.00000001"))
    return min(abs(primary - secondary) / denominator, Decimal("1"))


def _validate_bounded_decimal(value: Decimal) -> None:
    decimal_tuple = value.as_tuple()
    exponent = decimal_tuple.exponent
    if (
        len(decimal_tuple.digits) > MAX_TRACE_DECIMAL_DIGITS
        or not isinstance(exponent, int)
        or abs(exponent) > 128
    ):
        raise ValueError("reconciliation decimal exceeds bounded precision")


def _selected_trace_candidate(
    trace: ReconciliationTrace,
) -> ReconciliationCandidateTrace | None:
    if trace.selected_provider == trace.primary.provider:
        return trace.primary
    if trace.selected_provider == trace.secondary.provider:
        return trace.secondary
    return None


def _validate_materialization_reconciliation(
    value: ProviderSnapshotMaterialization,
) -> None:
    trace = value.reconciliation_trace
    if trace is None:
        return
    selected = _selected_trace_candidate(trace)
    valid = (
        selected is not None
        and selected.provider == value.provider
        and selected.provider_version == value.provider_version
        and selected.endpoint == value.endpoint
        and selected.raw_content_hash == hashlib.sha256(value.raw_payload).hexdigest()
        and selected.normalized_content_hash
        == normalized_evidence_content_hash(value.evidence)
    )
    if trace.decision is ReconciliationOutcome.SELECTED_BOTH_EMPTY:
        valid = (
            valid
            and value.observation.state is ProviderDataState.LEGITIMATE_EMPTY
            and not value.evidence
        )
    if not valid:
        raise ValueError("reconciliation trace does not match selected materialization")


def _validate_manifest_reconciliation(
    value: CanonicalDatasetSnapshotManifest,
) -> None:
    trace = value.reconciliation_trace
    if trace is None:
        return
    selected = _selected_trace_candidate(trace)
    valid = (
        trace.policy_id == value.provider_policy_id
        and selected is not None
        and selected.provider == value.provider
        and selected.provider_version == value.provider_version
        and selected.endpoint == value.endpoint
        and selected.raw_content_hash == value.raw_artifact_hash
        and selected.normalized_content_hash
        == normalized_evidence_content_hash(value.entries)
    )
    if trace.decision is ReconciliationOutcome.SELECTED_BOTH_EMPTY:
        valid = (
            valid
            and value.provider_state is ProviderDataState.LEGITIMATE_EMPTY
            and not value.entries
        )
    if not valid:
        raise ValueError("manifest reconciliation trace is inconsistent")


def validate_materialization_limits(value: ProviderSnapshotMaterialization) -> None:
    """Reject resource-exhausting provider output before hashing or persistence."""

    if not isinstance(value.raw_payload, bytes):
        raise ValueError("snapshot raw payload must be bytes")
    if len(value.raw_payload) > MAX_RAW_PAYLOAD_BYTES:
        raise ValueError("snapshot raw payload exceeds size limit")
    if not isinstance(value.evidence, tuple):
        raise ValueError("snapshot evidence must be immutable")
    if len(value.evidence) > MAX_EVIDENCE_ITEMS:
        raise ValueError("snapshot evidence count exceeds limit")
    validate_provider_reasons(value.observation.reasons)
    _validate_observation_data(value.observation.data)
    _validate_normalized_total(value.evidence)


def validate_observation_evidence_link(
    value: ProviderSnapshotMaterialization,
) -> None:
    """Bind the selected observation to the exact normalized evidence payloads."""

    observed_hashes = sorted(
        stable_payload_hash(item) for item in value.observation.data
    )
    evidence_hashes = sorted(
        stable_payload_hash(item.payload) for item in value.evidence
    )
    if observed_hashes != evidence_hashes:
        raise ValueError("snapshot observation does not match normalized evidence")


def _validate_manifest_request(value: CanonicalDatasetSnapshotManifest) -> None:
    expected = stable_payload_hash(
        {
            "market": value.market,
            "capability": value.capability,
            "as_of": value.as_of.isoformat(),
            "query": value.query,
            "provider_policy_id": value.provider_policy_id,
        }
    )
    if value.request_hash != expected:
        raise ValueError("snapshot request hash does not match its canonical query")


def _validate_manifest_state(value: CanonicalDatasetSnapshotManifest) -> None:
    accepted = {
        ProviderDataState.AVAILABLE,
        ProviderDataState.LEGITIMATE_EMPTY,
        ProviderDataState.STALE,
        ProviderDataState.PARTIAL,
    }
    if value.provider_state not in accepted:
        raise ValueError("snapshot manifest cannot contain failed provider output")
    if value.provider_state is ProviderDataState.LEGITIMATE_EMPTY:
        if value.entries or value.completeness != 1:
            raise ValueError("legitimate-empty snapshot must be complete and empty")
    elif not value.entries:
        raise ValueError("non-empty provider state requires snapshot entries")
    if value.provider_state is ProviderDataState.AVAILABLE and value.completeness != 1:
        raise ValueError("available snapshot must be complete")
    if (
        value.provider_state is ProviderDataState.PARTIAL
        and not 0 < value.completeness < 1
    ):
        raise ValueError("partial snapshot must have partial completeness")
    if (
        value.provider_state in {ProviderDataState.STALE, ProviderDataState.PARTIAL}
        and not value.reasons
    ):
        raise ValueError("degraded snapshot state requires a reason")


def _validate_manifest_entries(value: CanonicalDatasetSnapshotManifest) -> None:
    entry_ids = tuple(item.evidence_id for item in value.entries)
    if len(entry_ids) != len(set(entry_ids)):
        raise ValueError("snapshot manifest evidence entries must be unique")
    if any(item.raw_artifact_hash != value.raw_artifact_hash for item in value.entries):
        raise ValueError("snapshot entries must reference the archived raw payload")
    if any(not _matching_provenance(item, value) for item in value.entries):
        raise ValueError("snapshot entry provider provenance is inconsistent")
    if any(not _point_in_time_safe(item, value) for item in value.entries):
        raise ValueError("snapshot entry is not proven point-in-time safe")


def _matching_provenance(
    item: SnapshotManifestEntry,
    value: CanonicalDatasetSnapshotManifest,
) -> bool:
    return (
        item.provider == value.provider
        and item.provider_version == value.provider_version
        and item.endpoint == value.endpoint
        and item.license_tag == value.license_tag
        and item.redistribution_tag == value.redistribution_tag
        and item.sensitivity is value.sensitivity
    )


def _point_in_time_safe(
    item: SnapshotManifestEntry,
    value: CanonicalDatasetSnapshotManifest,
) -> bool:
    timeline = item.timeline
    return (
        timeline.strict_point_in_time
        and timeline.availability_certainty is AvailabilityCertainty.PROVEN
        and timeline.available_at <= value.as_of
        and timeline.as_of <= value.as_of
        and timeline.observed_at <= value.provider_observed_at
    )


def normalized_payload_size(payload: object) -> int:
    """Return bounded canonical JSON size after validating its shape."""

    return _measure_json(
        payload, depth=0, remaining=MAX_NORMALIZED_ITEM_BYTES, active=set()
    )


def _validate_normalized_total(
    entries: tuple[MaterializedEvidence | SnapshotManifestEntry, ...],
) -> None:
    total = 0
    for item in entries:
        total += normalized_payload_size(item.payload)
        if total > MAX_NORMALIZED_TOTAL_BYTES:
            raise ValueError("snapshot normalized payload exceeds total size limit")


def validate_provider_reasons(reasons: tuple[str, ...]) -> None:
    if not isinstance(reasons, tuple):
        raise ValueError("snapshot provider reasons must be immutable")
    if len(reasons) > MAX_REASONS:
        raise ValueError("snapshot provider reason count exceeds limit")
    if any(not isinstance(reason, str) for reason in reasons):
        raise ValueError("snapshot provider reason is invalid")
    if any(not reason.strip() for reason in reasons):
        raise ValueError("snapshot provider reason must not be blank")
    if any(len(reason) > MAX_REASON_LENGTH for reason in reasons):
        raise ValueError("snapshot provider reason exceeds length limit")
    if any(_contains_control(reason) for reason in reasons):
        raise ValueError("snapshot provider reason contains control characters")


def _validate_observation_data(data: tuple[object, ...]) -> None:
    if not isinstance(data, tuple):
        raise ValueError("snapshot observation data must be immutable")
    if len(data) > MAX_EVIDENCE_ITEMS:
        raise ValueError("snapshot observation count exceeds limit")
    if any(not isinstance(item, dict) for item in data):
        raise ValueError("snapshot observation data must contain JSON objects")
    total = 0
    for item in data:
        total += normalized_payload_size(item)
        if total > MAX_NORMALIZED_TOTAL_BYTES:
            raise ValueError("snapshot observation payload exceeds total size limit")


def _measure_json(
    value: object,
    *,
    depth: int,
    remaining: int,
    active: set[int],
) -> int:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("snapshot normalized JSON nesting is too deep")
    if value is None or isinstance(value, (str, bool, int, float)):
        return _measure_scalar(value, remaining)
    if isinstance(value, list):
        return _measure_list(value, depth, remaining, active)
    if isinstance(value, dict):
        return _measure_map(value, depth, remaining, active)
    raise ValueError("snapshot normalized payload contains a non-JSON value")


def _measure_scalar(value: object, remaining: int) -> int:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("snapshot normalized numbers must be finite")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("snapshot normalized scalar is invalid") from error
    return _within_budget(len(encoded), remaining)


def _measure_list(
    value: list[object],
    depth: int,
    remaining: int,
    active: set[int],
) -> int:
    if len(value) > MAX_JSON_LIST_ITEMS:
        raise ValueError("snapshot normalized JSON list exceeds item limit")
    with_container = _enter_container(value, active)
    try:
        total = _within_budget(2 + max(0, len(value) - 1), remaining)
        for item in value:
            total += _measure_json(
                item,
                depth=depth + 1,
                remaining=remaining - total,
                active=with_container,
            )
        return total
    finally:
        active.remove(id(value))


def _measure_map(
    value: dict[object, object],
    depth: int,
    remaining: int,
    active: set[int],
) -> int:
    if len(value) > MAX_JSON_MAP_ITEMS:
        raise ValueError("snapshot normalized JSON object exceeds item limit")
    keys = _validated_keys(value)
    with_container = _enter_container(value, active)
    try:
        total = _within_budget(2 + max(0, len(keys) - 1), remaining)
        for key in keys:
            key_size = _measure_scalar(key, remaining - total)
            total += key_size + _within_budget(1, remaining - total - key_size)
            total += _measure_json(
                value[key],
                depth=depth + 1,
                remaining=remaining - total,
                active=with_container,
            )
        return total
    finally:
        active.remove(id(value))


def _validated_keys(value: dict[object, object]) -> tuple[str, ...]:
    keys: list[str] = []
    for key in value:
        if (
            not isinstance(key, str)
            or not key
            or len(key) > MAX_JSON_KEY_LENGTH
            or _contains_control(key)
        ):
            raise ValueError("snapshot normalized JSON key is invalid")
        keys.append(key)
    return tuple(sorted(keys))


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _enter_container(value: object, active: set[int]) -> set[int]:
    identity = id(value)
    if identity in active:
        raise ValueError("snapshot normalized JSON contains a cycle")
    active.add(identity)
    return active


def _within_budget(size: int, remaining: int) -> int:
    if size > remaining:
        raise ValueError("snapshot normalized evidence exceeds item size limit")
    return size
