"""Bounded offline replay source for canonical snapshot materialization."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from stonks_agent.adapters.market_data.replay import (
    ReplayDataset,
    ReplayFixtureEntry,
    ReplayFixtureIntegrityError,
    ReplayManifest,
    ReplayQuery,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.dataset_snapshot import (
    MAX_EVIDENCE_ITEMS,
    MAX_RAW_PAYLOAD_BYTES,
    MaterializedEvidence,
    ProviderSnapshotMaterialization,
    validate_provider_reasons,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.provider_policy import ProviderPolicy, ProviderRoute
from stonks_contracts.evidence import EvidenceKind, Sensitivity

_READ_CHUNK_BYTES = 64 * 1024


class _PayloadLimitError(ValueError):
    pass


class ReplaySnapshotMaterializationSource:
    """Convert an integrity-checked replay fixture into bounded snapshot input."""

    def __init__(self, manifest_path: Path, policy: ProviderPolicy) -> None:
        self._manifest_path = Path(manifest_path).resolve()
        self._manifest = _load_manifest(self._manifest_path)
        self._policy = policy

    def fetch(
        self,
        request: FetchDataRequest,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]:
        route = _replay_route(self._policy, request, provider_policy_id)
        if isinstance(route, Failure):
            return route
        entry = self._fixture_for(request)
        if isinstance(entry, Failure):
            return entry
        raw = self._read_verified(entry.value)
        if isinstance(raw, Failure):
            return raw
        dataset = _parse_dataset(raw.value, entry.value)
        if isinstance(dataset, Failure):
            return dataset
        observation = _observation(entry.value, dataset.value)
        if isinstance(observation, Failure):
            return observation
        if not observation.value.accepted(
            allow_stale=self._policy.allow_stale,
            allow_partial=self._policy.allow_partial,
        ):
            return _failure(
                ErrorCode.DATA_UNAVAILABLE,
                "Replay output is not accepted by provider policy",
            )
        return _materialization(entry.value, raw.value, observation.value)

    def _fixture_for(
        self,
        request: FetchDataRequest,
    ) -> Result[ReplayFixtureEntry]:
        try:
            query = ReplayQuery.model_validate(request.query)
        except ValidationError:
            return _failure(ErrorCode.INVALID_INPUT, "Replay query is invalid")
        matches = tuple(
            entry
            for entry in self._manifest.fixtures
            if _matches(entry, request, query)
        )
        if len(matches) != 1:
            return _failure(ErrorCode.DATA_UNAVAILABLE, "Replay fixture was not found")
        if matches[0].as_of > request.as_of:
            return _failure(
                ErrorCode.DATA_UNAVAILABLE,
                "Replay fixture is not available at request as-of",
            )
        try:
            validate_provider_reasons(matches[0].reasons)
        except ValueError:
            return _failure(ErrorCode.INVALID_INPUT, "Replay reasons exceed limits")
        return Success(matches[0])

    def _read_verified(self, entry: ReplayFixtureEntry) -> Result[bytes]:
        root = self._manifest_path.parent
        path = (root / entry.path).resolve()
        if not path.is_relative_to(root):
            return _failure(ErrorCode.CONFLICT, "Replay fixture path is invalid")
        try:
            payload, digest = _bounded_read_and_hash(path)
        except _PayloadLimitError:
            return _failure(
                ErrorCode.INVALID_INPUT,
                "Replay fixture exceeds raw payload size limit",
            )
        except OSError:
            return _failure(
                ErrorCode.DATA_UNAVAILABLE,
                "Replay fixture could not be read",
            )
        if not hmac.compare_digest(digest, entry.sha256):
            return _failure(ErrorCode.CONFLICT, "Replay fixture hash mismatch")
        return Success(payload)


def _load_manifest(path: Path) -> ReplayManifest:
    try:
        raw, _ = _bounded_read_and_hash(path)
        payload = yaml.safe_load(raw.decode("utf-8"))
        return ReplayManifest.model_validate(payload)
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        ValidationError,
        _PayloadLimitError,
    ) as error:
        raise ReplayFixtureIntegrityError(
            "replay snapshot manifest validation failed"
        ) from error


def _bounded_read_and_hash(path: Path) -> tuple[bytes, str]:
    size = path.stat().st_size
    if size < 0 or size > MAX_RAW_PAYLOAD_BYTES:
        raise _PayloadLimitError
    digest = hashlib.sha256()
    payload = bytearray()
    with path.open("rb") as stream:
        while True:
            remaining = MAX_RAW_PAYLOAD_BYTES - len(payload)
            chunk = stream.read(min(_READ_CHUNK_BYTES, remaining + 1))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_RAW_PAYLOAD_BYTES:
                raise _PayloadLimitError
            digest.update(chunk)
    return bytes(payload), digest.hexdigest()


def _parse_dataset(
    raw: bytes,
    entry: ReplayFixtureEntry,
) -> Result[ReplayDataset]:
    try:
        dataset = ReplayDataset.model_validate(json.loads(raw))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        RecursionError,
    ):
        return _failure(ErrorCode.CONFLICT, "Replay fixture content is invalid")
    if not _manifest_matches(entry, dataset):
        return _failure(ErrorCode.CONFLICT, "Replay manifest metadata mismatch")
    if (entry.state is ProviderDataState.CONFLICT) != bool(dataset.conflicts):
        return _failure(ErrorCode.CONFLICT, "Replay fixture conflict state is invalid")
    return Success(dataset)


def _observation(
    entry: ReplayFixtureEntry,
    dataset: ReplayDataset,
) -> Result[ProviderObservation[ReplayDataset]]:
    data = () if entry.state is ProviderDataState.CONFLICT else (dataset,)
    try:
        observation = ProviderObservation[ReplayDataset](
            state=entry.state,
            data=data,
            completeness=entry.completeness,
            reasons=entry.reasons,
            observed_at=entry.observed_at,
        )
    except ValidationError:
        return _failure(ErrorCode.CONFLICT, "Replay fixture state is invalid")
    return Success(observation)


def _materialization(
    entry: ReplayFixtureEntry,
    raw: bytes,
    observation: ProviderObservation[ReplayDataset],
) -> Result[ProviderSnapshotMaterialization]:
    evidence = _normalized_evidence(observation)
    if isinstance(evidence, Failure):
        return evidence
    generic = _evidence_observation(observation, evidence.value)
    try:
        value = ProviderSnapshotMaterialization(
            provider="replay",
            provider_version="fixture-manifest/1",
            endpoint="/v1/prices",
            raw_payload=raw,
            raw_media_type="application/json",
            license_tag=entry.license_tag,
            redistribution_tag=entry.redistribution_tag,
            sensitivity=Sensitivity.PUBLIC,
            observation=generic,
            evidence=evidence.value,
        )
    except ValidationError:
        return _failure(ErrorCode.INVALID_INPUT, "Replay snapshot input is invalid")
    return Success(value)


def _evidence_observation(
    observation: ProviderObservation[ReplayDataset],
    evidence: tuple[MaterializedEvidence, ...],
) -> ProviderObservation[object]:
    return ProviderObservation[object](
        state=observation.state,
        data=tuple(item.payload for item in evidence),
        completeness=observation.completeness,
        reasons=observation.reasons,
        observed_at=observation.observed_at,
    )


def _normalized_evidence(
    observation: ProviderObservation[ReplayDataset],
) -> Result[tuple[MaterializedEvidence, ...]]:
    if not observation.data:
        return Success(())
    dataset = observation.data[0]
    count = len(dataset.bars) + len(dataset.corporate_actions)
    if count > MAX_EVIDENCE_ITEMS:
        return _failure(ErrorCode.INVALID_INPUT, "Replay evidence count exceeds limit")
    try:
        bars = tuple(
            MaterializedEvidence(
                subject=dataset.symbol,
                kind=EvidenceKind.MARKET_DATA,
                payload=bar.model_dump(mode="json"),
                timeline=bar.timeline,
            )
            for bar in dataset.bars
        )
        actions = tuple(
            MaterializedEvidence(
                subject=dataset.symbol,
                kind=EvidenceKind.MARKET_DATA,
                payload=action.model_dump(mode="json"),
                timeline=action.timeline,
            )
            for action in dataset.corporate_actions
        )
    except ValidationError:
        return _failure(
            ErrorCode.INVALID_INPUT, "Replay normalized evidence is invalid"
        )
    return Success(bars + actions)


def _matches(
    entry: ReplayFixtureEntry,
    request: FetchDataRequest,
    query: ReplayQuery,
) -> bool:
    return (
        entry.market == request.market
        and entry.capability == request.capability
        and entry.symbol == query.symbol
        and entry.interval == query.interval
        and entry.scenario == query.scenario
    )


def _manifest_matches(entry: ReplayFixtureEntry, dataset: ReplayDataset) -> bool:
    return (
        entry.fixture_id == dataset.fixture_id
        and entry.market == dataset.market
        and entry.capability == dataset.capability
        and entry.symbol == dataset.symbol
        and entry.interval == dataset.interval
        and entry.as_of == dataset.as_of
    )


def _replay_route(
    policy: ProviderPolicy,
    request: FetchDataRequest,
    provider_policy_id: str,
) -> Result[ProviderRoute]:
    if (
        provider_policy_id != policy.policy_id
        or request.market != policy.market
        or request.capability != policy.capability
    ):
        return _failure(
            ErrorCode.CAPABILITY_DENIED,
            "Provider policy does not authorize replay request",
        )
    route = next(
        (item for item in policy.routes if item.provider == "replay"),
        None,
    )
    if (
        route is None
        or "/v1/prices" not in route.endpoints
        or route.freshness_seconds != 0
        or route.quota_floor != 0
    ):
        return _failure(
            ErrorCode.CONFIGURATION_INVALID,
            "Provider policy replay route is invalid",
        )
    return Success(route)


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
