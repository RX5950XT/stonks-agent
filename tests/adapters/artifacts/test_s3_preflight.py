from __future__ import annotations

import pytest

from stonks_agent.adapters.artifacts.s3_preflight import (
    verify_s3_bucket_controls,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success

BUCKET = "stonks-artifacts"
OWNER = "123456789012"


class Client:
    def __init__(
        self,
        *,
        versioning: str = "Enabled",
        object_lock: str = "Enabled",
        fail: bool = False,
    ) -> None:
        self.versioning = versioning
        self.object_lock = object_lock
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def get_bucket_versioning(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        if self.fail:
            raise RuntimeError("provider details")
        return {"Status": self.versioning}

    def get_object_lock_configuration(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        if self.fail:
            raise RuntimeError("provider details")
        return {"ObjectLockEnabled": self.object_lock}


def test_preflight_requires_actual_versioning_and_object_lock() -> None:
    client = Client()

    result = verify_s3_bucket_controls(
        client=client,
        bucket=BUCKET,
        expected_bucket_owner=OWNER,
    )

    assert result == Success(None)
    assert client.calls == [
        {"Bucket": BUCKET, "ExpectedBucketOwner": OWNER},
        {"Bucket": BUCKET, "ExpectedBucketOwner": OWNER},
    ]


@pytest.mark.parametrize(
    ("client", "code"),
    (
        (Client(versioning="Suspended"), ErrorCode.CONFIGURATION_INVALID),
        (Client(object_lock="Disabled"), ErrorCode.CONFIGURATION_INVALID),
        (Client(fail=True), ErrorCode.DATA_UNAVAILABLE),
    ),
)
def test_preflight_fails_closed_on_missing_control_or_outage(
    client: Client,
    code: ErrorCode,
) -> None:
    result = verify_s3_bucket_controls(
        client=client,
        bucket=BUCKET,
        expected_bucket_owner=OWNER,
    )

    assert isinstance(result, Failure)
    assert result.error.code is code
    assert "provider details" not in str(result.error)
