from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from stonks_agent.ports.service_credentials import ServiceBearerCredential


def test_service_bearer_credential_is_redacted_and_excluded_from_dump() -> None:
    raw = "short-lived-service-token"
    credential = ServiceBearerCredential(token=SecretStr(raw))

    assert credential.authorization_header() == f"Bearer {raw}"
    assert raw not in repr(credential)
    assert credential.model_dump() == {}


@pytest.mark.parametrize("raw", ["", " token", "token ", "token\nvalue", "x" * 4097])
def test_invalid_service_bearer_credential_fails_closed(raw: str) -> None:
    with pytest.raises(ValidationError):
        ServiceBearerCredential(token=SecretStr(raw))
