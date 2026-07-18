"""Verify deployed bucket invariants instead of trusting configuration claims."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)


@runtime_checkable
class S3BucketControlClientPort(Protocol):
    def get_bucket_versioning(self, **kwargs: object) -> object: ...

    def get_object_lock_configuration(self, **kwargs: object) -> object: ...


def verify_s3_bucket_controls(
    *,
    client: S3BucketControlClientPort,
    bucket: str,
    expected_bucket_owner: str,
) -> Result[None]:
    if (
        re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) is None
        or re.fullmatch(r"[0-9]{12}", expected_bucket_owner) is None
    ):
        return _failure(
            ErrorCode.CONFIGURATION_INVALID,
            "Artifact bucket authority is invalid",
        )
    request = {
        "Bucket": bucket,
        "ExpectedBucketOwner": expected_bucket_owner,
    }
    versioning = _call(client.get_bucket_versioning, request)
    object_lock = _call(client.get_object_lock_configuration, request)
    if isinstance(versioning, Failure) or isinstance(object_lock, Failure):
        return _failure(
            ErrorCode.DATA_UNAVAILABLE,
            "Artifact bucket controls could not be verified",
        )
    if (
        versioning.value.get("Status") != "Enabled"
        or object_lock.value.get("ObjectLockEnabled") != "Enabled"
    ):
        return _failure(
            ErrorCode.CONFIGURATION_INVALID,
            "Artifact bucket controls are not enabled",
        )
    return Success(None)


def _call(
    operation: object,
    request: Mapping[str, object],
) -> Result[Mapping[str, object]]:
    if not callable(operation):
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Artifact control is unavailable")
    try:
        response = operation(**request)
    except Exception:
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Artifact control is unavailable")
    if not isinstance(response, Mapping):
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Artifact control is unavailable")
    return Success(response)


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
