"""Authority-free preflight boundary for the isolated Kronos runtime."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    model_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    tokenizer_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class KronosWorker:
    def __init__(
        self, *, policy: KronosWorkerPolicy, loader: WarmOnceModelLoader
    ) -> None:
        self.policy = policy
        self.loader = loader

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


def _failure(code: str, message: str) -> WorkerOutcome[KronosPreflightResponse]:
    return WorkerOutcome(error=WorkerError(code=code, message=message))
