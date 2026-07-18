"""Secret-safe, read-only capabilities for one immutable artifact."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

from stonks_contracts.common import Sha256, UTCDateTime


class SignedArtifactReadCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_hash: Sha256
    method: Literal["GET"] = "GET"
    url: SecretStr = Field(repr=False, exclude=True)
    expires_at: UTCDateTime

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        parsed = urlsplit(raw)
        if (
            not 20 <= len(raw) <= 4_096
            or not raw.isascii()
            or any(character.isspace() or ord(character) < 32 for character in raw)
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.query
        ):
            raise ValueError("signed artifact URL is invalid")
        return value

    def reveal_url(self) -> str:
        """Reveal only at the final transport boundary."""

        return self.url.get_secret_value()
