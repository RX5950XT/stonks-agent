"""Transport-neutral references and non-serializing resolved secrets."""

from __future__ import annotations

import unicodedata

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class SecretRef(BaseModel):
    """A resolver-owned name; never the credential value itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,127}$")

    def __str__(self) -> str:
        return "SecretRef([REDACTED])"


class SecretAccessRequest(BaseModel):
    """An exact logical secret and its bounded application purpose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: SecretRef
    purpose: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")


class ResolvedSecret(BaseModel):
    """A versioned secret whose value cannot enter normal serialization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: SecretStr = Field(repr=False, exclude=True)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}$")

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        try:
            size = len(raw.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("resolved secret is invalid") from error
        if (
            not 1 <= size <= 65_536
            or raw.strip() != raw
            or any(unicodedata.category(character) == "Cc" for character in raw)
        ):
            raise ValueError("resolved secret is invalid")
        return value

    def reveal(self) -> str:
        """Reveal only at the final protocol boundary that needs the credential."""

        return self.value.get_secret_value()
