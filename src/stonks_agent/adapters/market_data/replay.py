"""Deterministic, integrity-checked canonical market-data replay adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_agent.domain.evidence import EvidenceTimeline
from stonks_agent.domain.market_data import OHLCBar
from stonks_contracts.common import (
    Currency,
    DecimalString,
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    UnitDecimal,
    UTCDateTime,
)


class ReplayFixtureIntegrityError(ValueError):
    """Raised when a replay manifest or fixture fails closed validation."""


class CorporateActionKind(StrEnum):
    SPLIT = "split"
    DIVIDEND = "dividend"


class ReplayCorporateAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: UUID
    kind: CorporateActionKind
    timeline: EvidenceTimeline
    effective_at: UTCDateTime
    ratio: PositiveDecimal | None = None
    cash_amount: NonNegativeDecimal | None = None
    currency: Currency | None = None

    @model_validator(mode="after")
    def validate_kind_payload(self) -> Self:
        if self.kind is CorporateActionKind.SPLIT:
            if (
                self.ratio is None
                or self.cash_amount is not None
                or self.currency is not None
            ):
                raise ValueError("split action requires only ratio")
        elif (
            self.cash_amount is None or self.currency is None or self.ratio is not None
        ):
            raise ValueError("dividend action requires cash_amount and currency")
        return self


class ReplayConflict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: NonEmptyString
    primary_source: NonEmptyString
    primary_value: DecimalString
    secondary_source: NonEmptyString
    secondary_value: DecimalString


class ReplayDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    market: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    instrument_id: UUID
    symbol: NonEmptyString
    interval: str = Field(pattern=r"^(?:1d|[1-9][0-9]*m)$")
    timezone: NonEmptyString
    as_of: UTCDateTime
    bars: tuple[OHLCBar, ...] = Field(min_length=1)
    corporate_actions: tuple[ReplayCorporateAction, ...] = ()
    conflicts: tuple[ReplayConflict, ...] = ()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be an IANA timezone") from error
        return value

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        event_times = tuple(bar.timeline.event_time for bar in self.bars)
        if any(current <= previous for previous, current in pairwise(event_times)):
            raise ValueError("replay bars must have strictly increasing event times")
        timelines = tuple(bar.timeline for bar in self.bars) + tuple(
            action.timeline for action in self.corporate_actions
        )
        if any(timeline.as_of != self.as_of for timeline in timelines):
            raise ValueError("all replay timelines must use the dataset as_of")
        return self


class ReplayFixtureEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    path: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*\.json$")
    market: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    symbol: NonEmptyString
    interval: str = Field(pattern=r"^(?:1d|[1-9][0-9]*m)$")
    scenario: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    state: ProviderDataState
    completeness: UnitDecimal
    reasons: tuple[str, ...] = ()
    as_of: UTCDateTime
    observed_at: UTCDateTime
    source: NonEmptyString
    source_time: UTCDateTime
    sha256: Sha256
    license_tag: NonEmptyString
    redistribution_tag: NonEmptyString
    tags: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_metadata(self) -> Self:
        if self.as_of > self.observed_at or self.source_time > self.observed_at:
            raise ValueError(
                "fixture source/as_of time cannot be later than observed_at"
            )
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("fixture tags must be unique")
        return self


class ReplayManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    generated_at: UTCDateTime
    fixtures: tuple[ReplayFixtureEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_uniqueness(self) -> Self:
        _require_unique((item.fixture_id for item in self.fixtures), "fixture IDs")
        _require_unique((item.path for item in self.fixtures), "fixture paths")
        query_keys = (
            (item.market, item.capability, item.symbol, item.interval, item.scenario)
            for item in self.fixtures
        )
        _require_unique(query_keys, "fixture query keys")
        return self


class LoadedReplayFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: ReplayFixtureEntry
    dataset: ReplayDataset


class ReplayQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: NonEmptyString
    interval: str = Field(pattern=r"^(?:1d|[1-9][0-9]*m)$")
    scenario: str = Field(default="canonical", pattern=r"^[a-z][a-z0-9_-]{0,31}$")


class ReplayMarketDataAdapter:
    """Read-only provider adapter backed by validated synthetic fixtures."""

    provider = "replay"

    def __init__(self, manifest_path: Path) -> None:
        self._manifest_path = Path(manifest_path).resolve()
        self._manifest = _load_manifest(self._manifest_path)
        self._fixtures = tuple(
            _load_fixture(self._manifest_path.parent, entry)
            for entry in self._manifest.fixtures
        )

    @property
    def manifest(self) -> ReplayManifest:
        return self._manifest

    @property
    def fixtures(self) -> tuple[LoadedReplayFixture, ...]:
        return self._fixtures

    def fetch(self, request: FetchDataRequest) -> ProviderObservation[ReplayDataset]:
        try:
            query = ReplayQuery.model_validate(request.query)
        except ValidationError:
            return _failure_observation(
                ProviderDataState.FETCH_FAILED,
                "invalid_replay_query",
                request.as_of,
            )
        matches = tuple(
            fixture
            for fixture in self._fixtures
            if _matches(fixture.entry, request, query)
        )
        if not matches:
            return _failure_observation(
                ProviderDataState.NOT_SUPPORTED,
                "replay_fixture_not_found",
                request.as_of,
            )
        fixture = matches[0]
        if fixture.entry.as_of > request.as_of:
            return _failure_observation(
                ProviderDataState.FETCH_FAILED,
                "replay_fixture_not_available_at_as_of",
                request.as_of,
            )
        return _fixture_observation(fixture)


def _load_manifest(path: Path) -> ReplayManifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ReplayManifest.model_validate(raw)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise ReplayFixtureIntegrityError(
            "replay manifest validation failed"
        ) from error


def _load_fixture(root: Path, entry: ReplayFixtureEntry) -> LoadedReplayFixture:
    path = (root / entry.path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ReplayFixtureIntegrityError(
            "replay fixture path escaped its manifest directory"
        )
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ReplayFixtureIntegrityError(
            f"replay fixture could not be read: {entry.fixture_id}"
        ) from error
    digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(digest, entry.sha256):
        raise ReplayFixtureIntegrityError(
            f"replay fixture SHA-256 mismatch: {entry.fixture_id}"
        )
    dataset = _parse_dataset(raw, entry.fixture_id)
    _validate_manifest_link(entry, dataset)
    fixture = LoadedReplayFixture(entry=entry, dataset=dataset)
    _validate_fixture_state(fixture)
    return fixture


def _parse_dataset(raw: bytes, fixture_id: str) -> ReplayDataset:
    try:
        payload = json.loads(raw)
        return ReplayDataset.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise ReplayFixtureIntegrityError(
            f"replay fixture validation failed: {fixture_id}"
        ) from error


def _validate_manifest_link(entry: ReplayFixtureEntry, dataset: ReplayDataset) -> None:
    linked = (
        entry.fixture_id == dataset.fixture_id
        and entry.market == dataset.market
        and entry.capability == dataset.capability
        and entry.symbol == dataset.symbol
        and entry.interval == dataset.interval
        and entry.as_of == dataset.as_of
    )
    if not linked:
        raise ReplayFixtureIntegrityError(
            f"manifest metadata mismatch: {entry.fixture_id}"
        )


def _validate_fixture_state(fixture: LoadedReplayFixture) -> None:
    entry = fixture.entry
    has_conflicts = bool(fixture.dataset.conflicts)
    if entry.state is ProviderDataState.CONFLICT and not has_conflicts:
        raise ReplayFixtureIntegrityError(
            f"conflict evidence is required: {entry.fixture_id}"
        )
    if entry.state is not ProviderDataState.CONFLICT and has_conflicts:
        raise ReplayFixtureIntegrityError(
            f"conflict evidence requires conflict state: {entry.fixture_id}"
        )
    try:
        _fixture_observation(fixture)
    except ValidationError as error:
        raise ReplayFixtureIntegrityError(
            f"fixture state validation failed: {entry.fixture_id}"
        ) from error


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


def _fixture_observation(
    fixture: LoadedReplayFixture,
) -> ProviderObservation[ReplayDataset]:
    entry = fixture.entry
    data = () if entry.state is ProviderDataState.CONFLICT else (fixture.dataset,)
    return ProviderObservation[ReplayDataset](
        state=entry.state,
        data=data,
        completeness=entry.completeness,
        reasons=entry.reasons,
        observed_at=entry.observed_at,
    )


def _failure_observation(
    state: ProviderDataState,
    reason: str,
    observed_at: datetime,
) -> ProviderObservation[ReplayDataset]:
    return ProviderObservation[ReplayDataset](
        state=state,
        data=(),
        completeness=Decimal("0"),
        reasons=(reason,),
        observed_at=observed_at,
    )


def _require_unique[T](values: Iterable[T], label: str) -> None:
    materialized: tuple[T, ...] = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"replay {label} must be unique")
