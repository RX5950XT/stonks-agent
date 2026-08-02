"""Immutable allowlist and spend limits for structured-output models."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from stonks_agent.domain.secrets import SecretRef
from stonks_contracts.common import NonNegativeDecimal


class ModelProvider(StrEnum):
    FAKE = "fake"
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_model: str = Field(pattern=r"^policy:[a-z][a-z0-9_.-]{0,127}$")
    provider: ModelProvider
    provider_model: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$",
    )
    environment: str = Field(
        default="production",
        pattern=r"^[a-z][a-z0-9_-]{0,31}$",
    )
    origin: str | None = Field(default=None, max_length=512)
    endpoint: str | None = Field(default=None, max_length=256)
    secret_ref: SecretRef | None = Field(default=None, repr=False)
    input_cost_per_million: NonNegativeDecimal
    cached_input_cost_per_million: NonNegativeDecimal
    cache_write_input_cost_per_million: NonNegativeDecimal
    output_cost_per_million: NonNegativeDecimal
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    max_total_tokens_per_request: int = Field(ge=1, le=10_000_000)
    max_cost_usd_per_request: NonNegativeDecimal
    max_transient_retries: int = Field(ge=0, le=3)
    max_repairs: int = Field(ge=0, le=2)
    timeout_seconds: Decimal = Field(gt=0, le=120, allow_inf_nan=False)
    max_response_bytes: int = Field(ge=1, le=16_777_216)

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.strip() != value or any(ord(character) < 32 for character in value):
            raise ValueError("model origin contains unsafe characters")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as error:
            raise ValueError("model origin is invalid") from error
        hostname = parsed.hostname
        host_literal = f"[{hostname}]" if hostname and ":" in hostname else hostname
        netloc = host_literal if port is None else f"{host_literal}:{port}"
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc.lower() != str(netloc).lower()
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("model origin must be a credential-free origin")
        if parsed.scheme == "http" and (
            hostname.lower() not in {"127.0.0.1", "localhost"} or port is None
        ):
            raise ValueError("plaintext model origin must be exact loopback")
        return value.rstrip("/")

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.startswith("/")
            or value.startswith("//")
            or "://" in value
            or "?" in value
            or "#" in value
        ):
            raise ValueError("model endpoint must be an allowlisted relative path")
        return value

    @model_validator(mode="after")
    def validate_provider_shape(self) -> Self:
        network_values = (self.origin, self.endpoint, self.secret_ref)
        if self.provider is ModelProvider.FAKE and any(
            value is not None for value in network_values
        ):
            raise ValueError("fake model route cannot have network or secret settings")
        if self.provider is not ModelProvider.FAKE and any(
            value is None for value in network_values
        ):
            raise ValueError("remote model route requires origin, endpoint, and secret")
        if (
            self.origin is not None
            and urlsplit(self.origin).scheme == "http"
            and self.environment not in {"local", "development"}
        ):
            raise ValueError(
                "plaintext model origin is restricted to local development"
            )
        return self


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1, le=1)
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    routes: tuple[ModelRoute, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_unique_models(self) -> Self:
        models = tuple(route.request_model for route in self.routes)
        if len(models) != len(set(models)):
            raise ValueError("request models must be unique")
        return self

    def resolve(self, request_model: str) -> ModelRoute:
        for route in self.routes:
            if route.request_model == request_model:
                return route
        raise LookupError("requested model is not allowlisted")


def load_model_policy(path: str | Path) -> ModelPolicy:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return ModelPolicy.model_validate(payload)
    except (OSError, yaml.YAMLError, ValidationError, TypeError) as error:
        raise ValueError("model policy file could not be loaded") from error
