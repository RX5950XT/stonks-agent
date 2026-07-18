from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from stonks_agent.domain.s3_credentials import S3CredentialBundle

NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)


def bundle(*, token: str | None) -> S3CredentialBundle:
    return S3CredentialBundle(
        access_key_id=SecretStr("runtime-access-key"),
        secret_access_key=SecretStr("runtime-secret-key"),
        session_token=SecretStr(token) if token is not None else None,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        source="workload-identity",
        version="version-1",
    )


@pytest.mark.parametrize("token", ("runtime-session-token", None))
def test_atomic_bundle_excludes_all_credential_values(token: str | None) -> None:
    value = bundle(token=token)

    rendered = repr(value)
    payload = value.model_dump(mode="json")

    assert "runtime-access-key" not in rendered
    assert "runtime-secret-key" not in rendered
    assert token is None or token not in rendered
    assert set(payload) == {"issued_at", "expires_at", "source", "version"}


@pytest.mark.parametrize(
    "update",
    (
        {"access_key_id": SecretStr(" leading")},
        {"secret_access_key": SecretStr("line\nbreak")},
        {"session_token": SecretStr("")},
        {"expires_at": datetime(2026, 7, 18, 12)},
        {"source": "ENV"},
    ),
)
def test_bundle_rejects_unsafe_or_ambiguous_values(
    update: dict[str, object],
) -> None:
    values = {
        "access_key_id": SecretStr("runtime-access-key"),
        "secret_access_key": SecretStr("runtime-secret-key"),
        "session_token": SecretStr("runtime-session-token"),
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
        "source": "workload-identity",
        "version": "version-1",
    }
    values.update(update)

    with pytest.raises(ValidationError):
        S3CredentialBundle.model_validate(values)


def test_bundle_lifetime_is_positive_and_bounded() -> None:
    for expires_at in (NOW, NOW + timedelta(hours=12, seconds=1)):
        with pytest.raises(ValidationError):
            S3CredentialBundle(
                access_key_id=SecretStr("runtime-access-key"),
                secret_access_key=SecretStr("runtime-secret-key"),
                issued_at=NOW,
                expires_at=expires_at,
                source="workload-identity",
                version="version-1",
            )
