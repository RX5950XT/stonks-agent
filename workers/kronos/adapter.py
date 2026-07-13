"""Authority-free preflight boundary for the isolated Kronos runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from time import monotonic
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_contracts.kronos import (
    KronosForecastPath,
    KronosRuntimeIdentity,
    KronosWorkerRequest,
    KronosWorkerResponse,
    KronosWorkerResult,
    VolumeQuality,
)
from workers.kronos.model_loader import WarmOnceModelLoader


class WorkerDeviceProfile(StrEnum):
    CPU = "cpu"
    CUDA = "cuda"


class WorkerEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: WorkerDeviceProfile
    model_root: Path

    @model_validator(mode="after")
    def validate_model_root(self) -> WorkerEnvironment:
        raw = str(self.model_root)
        root_relative_posix_mount = raw.startswith(("/", "\\"))
        if (
            not self.model_root.is_absolute() and not root_relative_posix_mount
        ) or "://" in raw:
            raise ValueError("model root must be an absolute local path")
        return self


class KronosPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    profile: WorkerDeviceProfile
    upstream_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    model_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    tokenizer_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class KronosPreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    ready: bool
    worker_version: str
    profile: WorkerDeviceProfile
    upstream_commit: str
    model_revision: str
    tokenizer_revision: str
    manifest_hash: str


class WorkerError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class WorkerOutcome[T](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    value: T | None = None
    error: WorkerError | None = None

    @model_validator(mode="after")
    def validate_exclusive(self) -> WorkerOutcome[T]:
        if (self.value is None) == (self.error is None):
            raise ValueError("worker outcome must contain exactly one branch")
        return self


class KronosWorkerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_version: str = Field(pattern=r"^kronos-worker/[0-9]+\.[0-9]+\.[0-9]+$")
    profile: WorkerDeviceProfile
    upstream_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    model_id: str = Field(min_length=1, max_length=256)
    model_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    model_artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tokenizer_id: str = Field(min_length=1, max_length=256)
    tokenizer_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    tokenizer_artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    torch_version: str = Field(min_length=1, max_length=64)
    inference_code_version: str = Field(min_length=1, max_length=128)

    @property
    def runtime_identity(self) -> KronosRuntimeIdentity:
        return KronosRuntimeIdentity(
            worker_version=self.worker_version,
            upstream_commit=self.upstream_commit,
            model_id=self.model_id,
            model_revision=self.model_revision,
            model_artifact_hash=self.model_artifact_hash,
            tokenizer_id=self.tokenizer_id,
            tokenizer_revision=self.tokenizer_revision,
            tokenizer_artifact_hash=self.tokenizer_artifact_hash,
            manifest_hash=self.manifest_hash,
            runtime_hash=self.runtime_hash,
            device=self.profile.value,
            torch_version=self.torch_version,
            inference_code_version=self.inference_code_version,
        )


class KronosWorker:
    def __init__(
        self,
        *,
        policy: KronosWorkerPolicy,
        loader: WarmOnceModelLoader,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self.loader = loader
        self._clock = clock or (lambda: datetime.now(UTC))
        self._inference_lock = Lock()

    def preflight(
        self, request: KronosPreflightRequest
    ) -> WorkerOutcome[KronosPreflightResponse]:
        if not self.loader.ready:
            return _failure("model_not_ready", "Kronos model is not ready")
        expected = (
            self.policy.profile,
            self.policy.upstream_commit,
            self.policy.model_revision,
            self.policy.tokenizer_revision,
            self.policy.manifest_hash,
        )
        actual = (
            request.profile,
            request.upstream_commit,
            request.model_revision,
            request.tokenizer_revision,
            request.manifest_hash,
        )
        if actual != expected:
            return _failure(
                "runtime_mismatch", "Kronos runtime identity does not match"
            )
        return WorkerOutcome(
            value=KronosPreflightResponse(
                request_id=request.request_id,
                ready=True,
                worker_version=self.policy.worker_version,
                profile=self.policy.profile,
                upstream_commit=self.policy.upstream_commit,
                model_revision=self.policy.model_revision,
                tokenizer_revision=self.policy.tokenizer_revision,
                manifest_hash=self.policy.manifest_hash,
            )
        )

    def forecast(
        self, request: KronosWorkerRequest
    ) -> WorkerOutcome[KronosWorkerResponse]:
        if not self.loader.ready:
            return _failure("model_not_ready", "Kronos model is not ready")
        if self._clock() >= request.deadline:
            return _failure("deadline_expired", "Kronos request deadline expired")
        if request.runtime != self.policy.runtime_identity:
            return _failure(
                "runtime_mismatch", "Kronos runtime identity does not match"
            )
        runtime = self.loader.get()
        predict_path = getattr(runtime, "predict_path", None)
        if not callable(predict_path):
            return _failure("runtime_invalid", "Kronos runtime cannot forecast")
        started = monotonic()
        paths: list[KronosForecastPath] = []
        try:
            with self._inference_lock:
                for path_index, seed in enumerate(request.sampling.seeds):
                    if self._clock() >= request.deadline:
                        return _failure(
                            "deadline_expired", "Kronos request deadline expired"
                        )
                    points = tuple(predict_path(request, seed=seed))
                    if (
                        tuple(point.timestamp for point in points)
                        != request.future_timestamps
                    ):
                        return _failure(
                            "invalid_model_output",
                            "Kronos output does not match requested timestamps",
                        )
                    paths.append(
                        KronosForecastPath(
                            path_index=path_index,
                            seed=seed,
                            points=points,
                        )
                    )
            result = KronosWorkerResult(
                instrument_id=request.instrument_id,
                dataset_snapshot_id=request.dataset_snapshot_id,
                as_of=request.as_of,
                interval=request.interval,
                input_window_start=request.bars[0].event_time,
                input_window_end=request.bars[-1].event_time,
                future_timestamps=request.future_timestamps,
                input_last_close=request.bars[-1].close,
                input_volume_quality=_minimum_volume_quality(request),
                runtime=request.runtime,
                sampling=request.sampling,
                paths=tuple(paths),
                generated_at=self._clock(),
                latency_ms=max(0, round((monotonic() - started) * 1_000)),
                warnings=_volume_warnings(request),
            )
            response = KronosWorkerResponse(
                request_id=request.request_id,
                run_id=request.run_id,
                job_id=request.job_id,
                attempt_generation=request.attempt_generation,
                attempt_nonce=request.attempt_nonce,
                result_artifact_hash=result.payload_hash(),
                result=result,
            )
        except ValidationError:
            return _failure("invalid_model_output", "Kronos output is invalid")
        except Exception:
            return _failure("inference_failed", "Kronos inference failed")
        return WorkerOutcome(value=response)


def validate_worker_environment(environment: Mapping[str, str]) -> WorkerEnvironment:
    forbidden_fragments = (
        "DATABASE",
        "POSTGRES",
        "BROKER",
        "REDIS",
        "QUEUE",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "HF_TOKEN",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    )
    if any(
        any(fragment in key.upper() for fragment in forbidden_fragments)
        for key in environment
    ):
        raise ValueError("forbidden worker environment variable is present")
    return WorkerEnvironment(
        profile=WorkerDeviceProfile(environment.get("STONKS_KRONOS_PROFILE", "cpu")),
        model_root=Path(environment.get("STONKS_KRONOS_MODEL_ROOT", "/models")),
    )


def _minimum_volume_quality(request: KronosWorkerRequest) -> VolumeQuality:
    qualities = {bar.volume_quality for bar in request.bars}
    if VolumeQuality.MISSING in qualities:
        return VolumeQuality.MISSING
    if VolumeQuality.ESTIMATED in qualities:
        return VolumeQuality.ESTIMATED
    return VolumeQuality.OBSERVED


def _volume_warnings(request: KronosWorkerRequest) -> tuple[str, ...]:
    quality = _minimum_volume_quality(request)
    if quality is VolumeQuality.MISSING:
        return ("input_volume_missing",)
    if quality is VolumeQuality.ESTIMATED:
        return ("input_volume_estimated",)
    return ()


def _failure[T](code: str, message: str) -> WorkerOutcome[T]:
    return WorkerOutcome(error=WorkerError(code=code, message=message))
