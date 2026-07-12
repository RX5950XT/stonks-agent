"""Validated source and artifact provenance for canonical data."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from stonks_contracts.common import NonEmptyString, Sha256, UTCDateTime


class ProvenanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    provider_version: NonEmptyString
    endpoint: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$",
    )
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]*$",
    )
    source_url: str = Field(min_length=1, max_length=2048)
    raw_artifact_hash: Sha256
    payload_hash: Sha256
    observed_at: UTCDateTime
    license_tag: NonEmptyString
    redistribution_tag: NonEmptyString

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        has_forbidden_character = any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        if has_forbidden_character:
            raise ValueError(
                "source_url must not contain whitespace or control characters"
            )
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("source_url must be a credential-free HTTPS URL")
        return value

    @field_validator("endpoint", mode="before")
    @classmethod
    def validate_relative_endpoint(cls, value: object) -> object:
        if not isinstance(value, str) or value.startswith("//") or "://" in value:
            raise ValueError("endpoint must be an allowlisted relative path")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def raw_artifact_ref(self) -> str:
        return f"sha256:{self.raw_artifact_hash}"
