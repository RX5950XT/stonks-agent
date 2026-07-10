from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks_agent.domain.capabilities import (
    Capability,
    EgressPolicy,
    ProcessCapabilityPolicy,
    authorize_capability,
    authorize_egress,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success


def test_default_process_policy_denies_every_capability_and_egress() -> None:
    policy = ProcessCapabilityPolicy(profile="research-worker")

    capability = authorize_capability(policy, Capability.EXECUTION)
    egress = authorize_egress(policy, "https://api.example.com/v1/data")

    assert isinstance(capability, Failure)
    assert capability.error.code is ErrorCode.CAPABILITY_DENIED
    assert isinstance(egress, Failure)
    assert egress.error.code is ErrorCode.EGRESS_DENIED


def test_explicit_https_origin_can_be_allowed() -> None:
    policy = ProcessCapabilityPolicy(
        profile="provider-worker",
        allowed={Capability.NETWORK_EGRESS},
        egress=EgressPolicy(allowed_origins={"https://api.example.com"}),
    )

    result = authorize_egress(policy, "https://api.example.com/v1/data?symbol=AAPL")

    assert isinstance(result, Success)
    assert result.value.origin == "https://api.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1/data",
        "https://api.example.com.evil.test/v1/data",
        "https://user:password@api.example.com/v1/data",
        "https://127.0.0.1/admin",
        "file:///etc/passwd",
        "not-a-url",
    ],
)
def test_egress_policy_fails_closed_for_unsafe_or_unlisted_urls(url: str) -> None:
    policy = ProcessCapabilityPolicy(
        profile="provider-worker",
        allowed={Capability.NETWORK_EGRESS},
        egress=EgressPolicy(allowed_origins={"https://api.example.com"}),
    )

    result = authorize_egress(policy, url)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.EGRESS_DENIED


def test_capability_policy_rejects_unknown_external_fields() -> None:
    with pytest.raises(ValidationError):
        ProcessCapabilityPolicy.model_validate(
            {"profile": "worker", "allowed": [], "privileged": True}
        )
