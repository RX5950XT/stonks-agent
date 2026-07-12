"""Opaque secret references safe to retain in immutable domain policies."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SecretRef(BaseModel):
    """A resolver-owned name; never the credential value itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment_variable: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]{1,127}$",
        repr=False,
    )

    def __str__(self) -> str:
        return "SecretRef([REDACTED])"
