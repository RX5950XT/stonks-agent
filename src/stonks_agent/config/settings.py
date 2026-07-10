"""Fail-fast, paper-only settings."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.domain.errors import ErrorCode, StructuredError


class ExecutionMode(StrEnum):
    PAPER = "paper"


class SecretRef(BaseModel):
    """A name understood by a secret resolver; never a secret value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment_variable: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]{1,127}$",
        repr=False,
    )

    def __str__(self) -> str:
        return "SecretRef([REDACTED])"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_name: str = Field(default="stonks-agent", pattern=r"^[a-z][a-z0-9-]*$")
    environment: str = Field(default="local", pattern=r"^[a-z][a-z0-9_-]*$")
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    secret_refs: dict[str, SecretRef] = Field(
        default_factory=dict,
        exclude=True,
        repr=False,
    )

    def safe_snapshot(self) -> Mapping[str, object]:
        return MappingProxyType(self.model_dump(mode="json"))


class SettingsLoadError(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("Application configuration is invalid")


def load_settings(path: Path) -> Settings:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        return Settings.model_validate(payload)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise SettingsLoadError(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Application configuration is invalid",
                details={"file": path.name},
            )
        ) from error
