"""Browser-safe contracts for one session-scoped structured model connection."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)


class GuiModelPublicConfig(BaseModel):
    """Model route, price, and budgets that are safe to return to the browser."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(min_length=1, max_length=512)
    endpoint: str = Field(
        default="/v1/chat/completions",
        min_length=1,
        max_length=256,
    )
    model_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$",
    )
    input_cost_per_million: Decimal = Field(default=Decimal("1.25"), ge=0)
    cached_input_cost_per_million: Decimal = Field(default=Decimal("0.625"), ge=0)
    cache_write_input_cost_per_million: Decimal = Field(default=Decimal("1.25"), ge=0)
    output_cost_per_million: Decimal = Field(default=Decimal("5"), ge=0)
    max_output_tokens: int = Field(default=4_096, ge=1, le=1_000_000)
    max_total_tokens: int = Field(default=32_768, ge=1, le=10_000_000)
    max_cost_usd: Decimal = Field(default=Decimal("1"), ge=0)
    max_transient_retries: int = Field(default=1, ge=0, le=3)
    max_repairs: int = Field(default=1, ge=0, le=2)
    timeout_seconds: Decimal = Field(default=Decimal("30"), gt=0, le=120)
    max_response_bytes: int = Field(
        default=1_048_576,
        ge=1,
        le=16_777_216,
    )


class ConfigureGuiModelSettings(GuiModelPublicConfig):
    """One transient browser submission; secret serialization stays masked."""

    api_key: SecretStr = Field(min_length=1, max_length=4_096, repr=False)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if secret.strip() != secret or any(
            ord(character) < 33 or ord(character) > 126 for character in secret
        ):
            raise ValueError("model API key is invalid")
        return value


class GuiModelConnectionTest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_model: str = Field(min_length=1, max_length=256)
    input_tokens: int = Field(ge=0, le=10_000_000)
    output_tokens: int = Field(ge=0, le=1_000_000)
    cost_usd: Decimal = Field(ge=0)
    elapsed_ms: int = Field(ge=0, le=120_000)
    tested_at: datetime


class GuiModelSettingsView(BaseModel):
    """Secret-free current state; `configured` does not imply trading authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["configured", "unconfigured", "unavailable"]
    source: Literal["environment", "session", "none"]
    detail: str = Field(min_length=1, max_length=256)
    api_key_configured: bool
    verified: bool
    generation: int = Field(ge=0)
    config: GuiModelPublicConfig | None = None
    connection_test: GuiModelConnectionTest | None = None
    updated_at: datetime | None = None
