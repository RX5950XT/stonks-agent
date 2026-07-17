from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from stonks_agent.domain.errors import Result
from stonks_agent.domain.secrets import (
    ResolvedSecret,
    SecretAccessRequest,
    SecretRef,
)
from stonks_agent.ports.secret_provider import SecretProvider


def test_secret_contracts_are_frozen_transport_neutral_and_non_serializing() -> None:
    reference = SecretRef(name="openai_api_key")
    request = SecretAccessRequest(
        reference=reference,
        purpose="llm.openai",
    )
    resolved = ResolvedSecret(
        value=SecretStr("arbitrary-sensitive-value"),
        version="version-42",
    )

    assert request.reference == reference
    assert resolved.reveal() == "arbitrary-sensitive-value"
    assert resolved.model_dump() == {"version": "version-42"}
    rendered = repr(resolved) + resolved.model_dump_json()
    assert "arbitrary-sensitive-value" not in rendered
    assert "openai_api_key" in json.dumps(reference.model_dump())
    with pytest.raises(ValidationError):
        request.purpose = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (SecretRef, {"name": "UPPERCASE"}),
        (SecretRef, {"name": "../secret"}),
        (
            SecretAccessRequest,
            {"reference": {"name": "valid_name"}, "purpose": "invalid purpose"},
        ),
        (ResolvedSecret, {"value": SecretStr(""), "version": "v1"}),
        (ResolvedSecret, {"value": SecretStr("   "), "version": "v1"}),
        (ResolvedSecret, {"value": SecretStr(" leading"), "version": "v1"}),
        (ResolvedSecret, {"value": SecretStr("trailing "), "version": "v1"}),
        (ResolvedSecret, {"value": SecretStr("line\nbreak"), "version": "v1"}),
        (ResolvedSecret, {"value": SecretStr("unicode\u0085control"), "version": "v1"}),
        (ResolvedSecret, {"value": SecretStr("value"), "version": "bad version"}),
    ],
)
def test_secret_contracts_reject_invalid_or_ambiguous_values(
    model: type[object],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model(**payload)  # type: ignore[call-arg]


def test_secret_provider_is_runtime_checkable() -> None:
    class Provider:
        def resolve(self, request: SecretAccessRequest) -> Result[ResolvedSecret]:
            raise NotImplementedError

    assert isinstance(Provider(), SecretProvider)


def test_resolved_secret_rejects_oversized_value_without_echoing_it() -> None:
    value = "x" * 65_537

    with pytest.raises(ValidationError) as raised:
        ResolvedSecret(value=SecretStr(value), version="v1")

    assert value not in str(raised.value)
