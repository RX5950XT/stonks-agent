from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError

from stonks_agent.domain.artifact_capability import SignedArtifactReadCapability

HASH = "a" * 64
EXPIRES_AT = datetime(2026, 7, 17, 12, 5, tzinfo=UTC)
URL = (
    f"https://objects.example/artifacts/objects/{HASH[:2]}/{HASH}"
    "?X-Amz-Expires=300&X-Amz-Signature=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)


def test_read_capability_is_frozen_and_excludes_bearer_url_from_output() -> None:
    capability = SignedArtifactReadCapability(
        content_hash=HASH,
        url=SecretStr(URL),
        expires_at=EXPIRES_AT,
    )

    assert capability.method == "GET"
    assert capability.reveal_url() == URL
    assert URL not in repr(capability)
    assert "url" not in capability.model_dump()
    assert "url" not in capability.model_dump(mode="json")
    with pytest.raises(ValidationError):
        capability.content_hash = "b" * 64


@pytest.mark.parametrize(
    "overrides",
    (
        {"method": "PUT"},
        {"expires_at": datetime(2026, 7, 17, 12, 5)},
        {"url": SecretStr("https://user@objects.example/artifact?signature=x")},
        {"url": SecretStr("https://objects.example/artifact#fragment")},
        {"unknown": True},
    ),
)
def test_read_capability_rejects_unsafe_or_unknown_fields(
    overrides: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "content_hash": HASH,
        "url": SecretStr(URL),
        "expires_at": EXPIRES_AT,
    }
    payload.update(overrides)

    with pytest.raises(ValidationError):
        SignedArtifactReadCapability.model_validate(payload)
