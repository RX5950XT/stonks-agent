"""Strict S3-compatible artifact-storage policy."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.domain.artifact_retention import (
    ArtifactEncryption,
    ArtifactRetentionMode,
)
from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_contracts.common import SchemaVersion
from stonks_contracts.evidence import Sensitivity


class S3AddressingStyle(StrEnum):
    PATH = "path"
    VIRTUAL = "virtual"


class S3AuthMode(StrEnum):
    WORKLOAD_IDENTITY = "workload_identity"
    STATIC_TEST = "static_test"


class S3EncryptionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: ArtifactEncryption
    kms_key_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@-]{2,511}$",
    )

    @model_validator(mode="after")
    def validate_key(self) -> Self:
        if (self.algorithm is ArtifactEncryption.KMS) != (self.kms_key_id is not None):
            raise ValueError("KMS encryption requires exactly one key identifier")
        return self


class ArtifactRetentionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ArtifactRetentionMode
    days: int = Field(ge=1, le=36_500)


class ArtifactStorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: SchemaVersion
    backend: str = Field(pattern=r"^s3$")
    endpoint_url: str = Field(min_length=8, max_length=2_048)
    region: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$",
    )
    bucket: str = Field(
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
    )
    expected_bucket_owner: str = Field(pattern=r"^[0-9]{12}$")
    prefix: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._/-]{0,127}$",
    )
    addressing_style: S3AddressingStyle
    auth_mode: S3AuthMode
    max_size_bytes: int = Field(ge=1, le=1_073_741_824)
    request_timeout_seconds: int = Field(ge=1, le=120)
    presign_ttl_seconds: int = Field(ge=1, le=900)
    versioning_required: bool
    object_lock_required: bool
    allow_insecure_loopback: bool
    encryption: S3EncryptionConfig
    retention: dict[Sensitivity, ArtifactRetentionRule]

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        parsed = urlsplit(self.endpoint_url)
        loopback = _is_loopback(parsed.hostname)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not parsed.hostname
        ):
            raise ValueError("artifact endpoint must be an exact HTTP origin")
        insecure = parsed.scheme == "http"
        test_mode = self.auth_mode is S3AuthMode.STATIC_TEST
        if self.allow_insecure_loopback != insecure or (insecure and not loopback):
            raise ValueError("insecure artifact endpoint is restricted to loopback")
        if test_mode and not loopback:
            raise ValueError(
                "static artifact credentials are restricted to loopback tests"
            )
        if self.encryption.algorithm is ArtifactEncryption.NONE:
            raise ValueError("unencrypted artifact storage is unsupported")
        if self.addressing_style is not S3AddressingStyle.PATH:
            raise ValueError("only path-style S3 addressing is supported")
        if not self.versioning_required or not self.object_lock_required:
            raise ValueError("artifact bucket requires versioning and object lock")
        if set(self.retention) != set(Sensitivity):
            raise ValueError("retention policy must cover every sensitivity")
        if not _safe_prefix(self.prefix):
            raise ValueError("artifact prefix is invalid")
        return self


class ArtifactStorageConfigError(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("Artifact storage configuration is invalid")


def load_artifact_storage_config(path: Path) -> ArtifactStorageConfig:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ArtifactStorageConfig.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise ArtifactStorageConfigError(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Artifact storage configuration is invalid",
                details={"file": path.name},
            )
        ) from error


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _safe_prefix(prefix: str) -> bool:
    segments = prefix.split("/")
    return all(segment not in {"", ".", ".."} for segment in segments)
