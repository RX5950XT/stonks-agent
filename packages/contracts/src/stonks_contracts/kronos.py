"""Point-in-time, path-retaining contracts for the isolated Kronos worker."""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    ContractModel,
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    UnitDecimal,
    UTCDateTime,
)

type KronosProfile = Literal["cpu", "cuda"]
type KronosInterval = Literal["1d"]


class VolumeQuality(StrEnum):
    """How the volume input was obtained before inference."""

    OBSERVED = "observed"
    ESTIMATED = "estimated"
    MISSING = "missing"


class KronosBar(ContractModel):
    """A point-in-time OHLCV bar admitted to a forecast request."""

    event_time: UTCDateTime
    available_at: UTCDateTime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal | None
    amount: NonNegativeDecimal | None = None
    volume_quality: VolumeQuality

    @model_validator(mode="after")
    def validate_market_invariants(self) -> Self:
        if self.low > self.high or not (
            self.low <= self.open <= self.high and self.low <= self.close <= self.high
        ):
            raise ValueError("OHLC must satisfy low <= open/close <= high")
        if self.available_at < self.event_time:
            raise ValueError("available_at cannot be earlier than event_time")
        if (self.volume is None) != (self.volume_quality is VolumeQuality.MISSING):
            raise ValueError("missing volume and volume quality must agree")
        if self.volume is None and self.amount is not None:
            raise ValueError("amount must be missing when volume is missing")
        return self


class KronosSamplingPolicy(ContractModel):
    """Explicit deterministic path seeds; the worker runs one seed at a time."""

    seed_policy: Literal["explicit-sequential-v1"]
    seeds: tuple[int, ...] = Field(min_length=1, max_length=32)
    temperature: PositiveDecimal = Field(le=5)
    top_k: int = Field(ge=0, le=4_096)
    top_p: UnitDecimal

    @model_validator(mode="after")
    def validate_seeds_and_probability(self) -> Self:
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("sampling seeds must be unique")
        if any(seed < 0 or seed > 2**63 - 1 for seed in self.seeds):
            raise ValueError("sampling seeds must be unsigned 63-bit integers")
        if self.top_p <= 0:
            raise ValueError("top_p must be greater than zero")
        return self

    @property
    def path_count(self) -> int:
        return len(self.seeds)


class KronosRuntimeIdentity(ContractModel):
    """Exact code, model, tokenizer, manifest, and runtime identity."""

    worker_version: NonEmptyString = Field(max_length=128)
    upstream_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    model_id: NonEmptyString = Field(max_length=256)
    model_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    model_artifact_hash: Sha256
    tokenizer_id: NonEmptyString = Field(max_length=256)
    tokenizer_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    tokenizer_artifact_hash: Sha256
    manifest_hash: Sha256
    runtime_hash: Sha256
    device: KronosProfile
    torch_version: NonEmptyString = Field(max_length=64)
    inference_code_version: NonEmptyString = Field(max_length=128)


class KronosWorkerRequest(ContractModel):
    """Lease-fenced request containing only data and inference authority."""

    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    profile: KronosProfile
    instrument_id: UUID
    mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    dataset_snapshot_id: UUID
    snapshot_artifact_ref: ArtifactRef
    data_hash: Sha256
    as_of: UTCDateTime
    interval: KronosInterval
    bars: tuple[KronosBar, ...] = Field(min_length=2, max_length=512)
    future_timestamps: tuple[UTCDateTime, ...] = Field(min_length=1, max_length=256)
    runtime: KronosRuntimeIdentity
    sampling: KronosSamplingPolicy
    deadline: UTCDateTime

    @model_validator(mode="after")
    def validate_scope_and_time(self) -> Self:
        event_times = tuple(bar.event_time for bar in self.bars)
        if any(current <= previous for previous, current in pairwise(event_times)):
            raise ValueError("bar event times must be strictly increasing and unique")
        if any(bar.event_time > self.as_of or bar.available_at > self.as_of for bar in self.bars):
            raise ValueError("future or unavailable bars are not allowed")
        if any(current <= previous for previous, current in pairwise(self.future_timestamps)):
            raise ValueError("future timestamps must be strictly increasing and unique")
        if self.future_timestamps[0] <= max(self.as_of, event_times[-1]):
            raise ValueError("forecast timestamps must be later than the research cutoff")
        if self.deadline <= self.as_of:
            raise ValueError("deadline must be later than as_of")
        if self.profile != self.runtime.device:
            raise ValueError("worker profile must match the runtime device")
        return self

    @property
    def horizon_bars(self) -> int:
        return len(self.future_timestamps)


class KronosForecastPoint(ContractModel):
    """One raw forecast bar in one stochastic path."""

    timestamp: UTCDateTime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal
    amount: NonNegativeDecimal

    @model_validator(mode="after")
    def validate_ohlc(self) -> Self:
        if self.low > self.high or not (
            self.low <= self.open <= self.high and self.low <= self.close <= self.high
        ):
            raise ValueError("OHLC must satisfy low <= open/close <= high")
        return self


class KronosForecastPath(ContractModel):
    """An unaggregated forecast path tied to one explicit seed."""

    path_index: int = Field(ge=0)
    seed: int = Field(ge=0, le=2**63 - 1)
    points: tuple[KronosForecastPoint, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        timestamps = tuple(point.timestamp for point in self.points)
        if any(current <= previous for previous, current in pairwise(timestamps)):
            raise ValueError("path timestamps must be strictly increasing and unique")
        return self


def _validate_paths(
    *,
    paths: tuple[KronosForecastPath, ...],
    sampling: KronosSamplingPolicy,
    future_timestamps: tuple[object, ...],
) -> None:
    if len(paths) != sampling.path_count:
        raise ValueError("one retained path is required for every seed")
    if tuple(path.path_index for path in paths) != tuple(range(len(paths))):
        raise ValueError("path indices must be contiguous and ordered")
    if tuple(path.seed for path in paths) != sampling.seeds:
        raise ValueError("path seeds must exactly match the sampling policy")
    if any(tuple(point.timestamp for point in path.points) != future_timestamps for path in paths):
        raise ValueError("every path must exactly match the requested forecast timestamps")


class KronosWorkerResult(ContractModel):
    """Raw, replayable worker output before core forecast mapping."""

    instrument_id: UUID
    dataset_snapshot_id: UUID
    as_of: UTCDateTime
    interval: KronosInterval
    input_window_start: UTCDateTime
    input_window_end: UTCDateTime
    future_timestamps: tuple[UTCDateTime, ...] = Field(min_length=1, max_length=256)
    input_last_close: PositiveDecimal
    input_volume_quality: VolumeQuality
    runtime: KronosRuntimeIdentity
    sampling: KronosSamplingPolicy
    paths: tuple[KronosForecastPath, ...] = Field(min_length=1, max_length=32)
    generated_at: UTCDateTime
    latency_ms: int = Field(ge=0)
    warnings: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.input_window_end < self.input_window_start:
            raise ValueError("input window end cannot precede its start")
        if self.input_window_end > self.as_of:
            raise ValueError("input window cannot extend beyond as_of")
        if self.generated_at < self.as_of:
            raise ValueError("generated_at cannot precede as_of")
        _validate_paths(
            paths=self.paths,
            sampling=self.sampling,
            future_timestamps=self.future_timestamps,
        )
        return self


class KronosWorkerResponse(ContractModel):
    """Lease-fenced response whose raw result hash is verified at ingress."""

    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    result_artifact_hash: Sha256
    result: KronosWorkerResult

    @model_validator(mode="after")
    def validate_result_hash(self) -> Self:
        if self.result_artifact_hash != self.result.payload_hash():
            raise ValueError("worker result artifact hash is invalid")
        return self


class KronosSamplePathsArtifact(ContractModel):
    """Lease-secret-free stochastic output used as the replay starting point."""

    request_id: UUID
    instrument_id: UUID
    dataset_snapshot_id: UUID
    as_of: UTCDateTime
    interval: KronosInterval
    input_window_start: UTCDateTime
    input_window_end: UTCDateTime
    input_last_close: PositiveDecimal
    input_volume_quality: VolumeQuality
    runtime: KronosRuntimeIdentity
    sampling: KronosSamplingPolicy
    future_timestamps: tuple[UTCDateTime, ...] = Field(min_length=1, max_length=256)
    paths: tuple[KronosForecastPath, ...] = Field(min_length=1, max_length=32)
    generated_at: UTCDateTime
    latency_ms: int = Field(ge=0)
    warnings: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if self.input_window_end < self.input_window_start:
            raise ValueError("input window end cannot precede its start")
        if self.input_window_end > self.as_of or self.generated_at < self.as_of:
            raise ValueError("artifact timeline is invalid")
        _validate_paths(
            paths=self.paths,
            sampling=self.sampling,
            future_timestamps=self.future_timestamps,
        )
        return self
