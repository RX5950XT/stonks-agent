from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from stonks_agent.config.artifacts import (
    ArtifactStorageConfig,
    ArtifactStorageConfigError,
    S3AddressingStyle,
    S3AuthMode,
    S3EncryptionConfig,
    load_artifact_storage_config,
)
from stonks_agent.domain.artifact_retention import (
    ArtifactEncryption,
    ArtifactRetentionMode,
)
from stonks_contracts.evidence import Sensitivity

ROOT = Path(__file__).resolve().parents[2]


def valid_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "backend": "s3",
        "endpoint_url": "https://s3.example.invalid",
        "region": "ap-northeast-1",
        "bucket": "stonks-artifacts",
        "expected_bucket_owner": "123456789012",
        "prefix": "stonks/v1",
        "addressing_style": "path",
        "auth_mode": "workload_identity",
        "max_size_bytes": 67_108_864,
        "request_timeout_seconds": 30,
        "presign_ttl_seconds": 300,
        "versioning_required": True,
        "object_lock_required": True,
        "allow_insecure_loopback": False,
        "encryption": {"algorithm": "AES256", "kms_key_id": None},
        "retention": {
            "public": {"mode": "governance", "days": 30},
            "internal": {"mode": "governance", "days": 365},
            "restricted": {"mode": "compliance", "days": 2_555},
        },
    }


def test_repository_config_is_strict_and_complete() -> None:
    value = load_artifact_storage_config(ROOT / "config" / "artifacts.yaml")

    assert value.backend == "s3"
    assert value.addressing_style is S3AddressingStyle.PATH
    assert value.auth_mode is S3AuthMode.WORKLOAD_IDENTITY
    assert value.encryption.algorithm is ArtifactEncryption.AES256
    assert set(value.retention) == set(Sensitivity)
    assert (
        value.retention[Sensitivity.RESTRICTED].mode is ArtifactRetentionMode.COMPLIANCE
    )


@pytest.mark.parametrize(
    "update",
    (
        {"unknown": True},
        {"endpoint_url": "https://user:password@s3.example.invalid"},
        {"endpoint_url": "https://s3.example.invalid/path"},
        {"endpoint_url": "http://s3.example.invalid"},
        {"bucket": "../escape"},
        {"expected_bucket_owner": "owner"},
        {"prefix": "stonks//v1"},
        {"prefix": "../stonks"},
        {"presign_ttl_seconds": 901},
        {"max_size_bytes": 0},
        {"versioning_required": False},
        {"object_lock_required": False},
        {"auth_mode": "environment"},
        {"addressing_style": "virtual"},
    ),
)
def test_config_rejects_unsafe_or_unbounded_values(update: dict[str, object]) -> None:
    payload = valid_payload()
    payload.update(update)

    with pytest.raises(ValidationError):
        ArtifactStorageConfig.model_validate(payload)


def test_kms_requires_an_explicit_nonsecret_key_identifier() -> None:
    with pytest.raises(ValidationError):
        S3EncryptionConfig(algorithm=ArtifactEncryption.KMS, kms_key_id=None)

    value = S3EncryptionConfig(
        algorithm=ArtifactEncryption.KMS,
        kms_key_id="alias/stonks-artifacts",
    )
    assert value.kms_key_id == "alias/stonks-artifacts"


def test_unencrypted_mode_is_rejected_even_for_explicit_loopback_tests() -> None:
    payload = valid_payload()
    payload.update(
        {
            "endpoint_url": "http://127.0.0.1:19000",
            "auth_mode": "static_test",
            "allow_insecure_loopback": True,
            "encryption": {"algorithm": "none", "kms_key_id": None},
        }
    )

    with pytest.raises(ValidationError):
        ArtifactStorageConfig.model_validate(payload)


def test_static_test_credentials_allow_secure_loopback_without_insecure_flag() -> None:
    payload = valid_payload()
    payload.update(
        {
            "endpoint_url": "https://127.0.0.1:19000",
            "auth_mode": "static_test",
            "allow_insecure_loopback": False,
        }
    )

    value = ArtifactStorageConfig.model_validate(payload)

    assert value.auth_mode is S3AuthMode.STATIC_TEST
    assert value.allow_insecure_loopback is False


@pytest.mark.parametrize(
    ("endpoint", "allow_insecure"),
    (
        ("http://127.0.0.1:19000", False),
        ("https://127.0.0.1:19000", True),
        ("http://[::1]:19000", False),
    ),
)
def test_insecure_loopback_flag_must_exactly_match_http_scheme(
    endpoint: str,
    allow_insecure: bool,
) -> None:
    payload = valid_payload()
    payload.update(
        {
            "endpoint_url": endpoint,
            "auth_mode": "static_test",
            "allow_insecure_loopback": allow_insecure,
        }
    )

    with pytest.raises(ValidationError):
        ArtifactStorageConfig.model_validate(payload)


def test_retention_catalog_must_have_each_sensitivity_exactly_once() -> None:
    payload = valid_payload()
    retention = dict(payload["retention"])  # type: ignore[arg-type]
    retention.pop("restricted")
    payload["retention"] = retention

    with pytest.raises(ValidationError):
        ArtifactStorageConfig.model_validate(payload)


def test_loader_returns_public_safe_error(tmp_path: Path) -> None:
    path = tmp_path / "credential-bearing-name.yaml"
    path.write_text(yaml.safe_dump({"backend": "s3", "secret": "do-not-leak"}))

    with pytest.raises(ArtifactStorageConfigError) as captured:
        load_artifact_storage_config(path)

    assert captured.value.error.code.value == "configuration_invalid"
    assert "do-not-leak" not in str(captured.value)
