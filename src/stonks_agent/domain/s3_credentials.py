"""Short-lived, atomic credentials for one S3 operation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from stonks_agent.domain.errors import Result


class S3CredentialBundle(BaseModel):
    """One versioned credential set; values never enter normal serialization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_key_id: SecretStr = Field(repr=False, exclude=True)
    secret_access_key: SecretStr = Field(repr=False, exclude=True)
    session_token: SecretStr | None = Field(
        default=None,
        repr=False,
        exclude=True,
    )
    issued_at: datetime
    expires_at: datetime
    source: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}$")

    @field_validator("access_key_id", "secret_access_key")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if (
            not 1 <= len(raw) <= 4_096
            or raw.strip() != raw
            or not raw.isascii()
            or any(not 0x21 <= ord(character) <= 0x7E for character in raw)
        ):
            raise ValueError("S3 credential is invalid")
        return value

    @field_validator("session_token")
    @classmethod
    def validate_session_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            cls.validate_secret(value)
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("S3 credential expiry is invalid")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_lifetime(self) -> S3CredentialBundle:
        lifetime = self.expires_at - self.issued_at
        if not timedelta(0) < lifetime <= timedelta(hours=12):
            raise ValueError("S3 credential lifetime is invalid")
        return self

    def reveal(self) -> tuple[str, str, str | None]:
        """Reveal the atomic bundle only at the SigV4 boundary."""

        return (
            self.access_key_id.get_secret_value(),
            self.secret_access_key.get_secret_value(),
            (
                self.session_token.get_secret_value()
                if self.session_token is not None
                else None
            ),
        )


@runtime_checkable
class S3CredentialProvider(Protocol):
    def resolve(self) -> Result[S3CredentialBundle]: ...
