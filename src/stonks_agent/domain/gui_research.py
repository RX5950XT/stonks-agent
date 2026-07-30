"""Bounded local-console research command and projection contracts."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from stonks_agent.domain.latest_market_data import BarInterval
from stonks_contracts.common import UTCDateTime

ResearchText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4_096),
]
ResearchProfileId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$",
    ),
]
ResearchStatus = Literal[
    "queued",
    "running",
    "degraded",
    "failed",
    "succeeded",
    "cancelled",
]


class GuiResearchCommand(BaseModel):
    """Server-owned command passed to a future durable workflow composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    interval: BarInterval
    profile: ResearchProfileId
    account_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$",
    )
    requested_at: UTCDateTime
    execution_mode: Literal["paper"] = "paper"

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class GuiResearchRunRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID


class GuiResearchClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: ResearchText
    evidence_ids: tuple[UUID, ...] = Field(min_length=1, max_length=64)


class GuiResearchUsageView(BaseModel):
    """Bounded cost/latency summary; prompts and provider envelopes stay sealed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    iterations: int = Field(ge=0, le=64)
    tool_calls: int = Field(ge=0, le=256)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    elapsed_ms: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class GuiResearchIssueView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")


class GuiResearchVersionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
    version: ResearchText


class GuiResearchHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    profile: ResearchProfileId
    status: ResearchStatus
    stage: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    as_of: UTCDateTime
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    issue_count: int = Field(default=0, ge=0, le=256)
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )
    updated_at: UTCDateTime


class GuiResearchHistoryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[GuiResearchHistoryItem, ...] = Field(max_length=20)


class GuiResearchEvidenceField(BaseModel):
    """One safe scalar field, never an arbitrary raw provider payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value: str = Field(min_length=1, max_length=512)


class GuiResearchEvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: UUID
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    source: ResearchText
    provider: ResearchText
    event_time: UTCDateTime
    available_at: UTCDateTime
    quality_status: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    completeness: Decimal = Field(ge=0, le=1)
    warnings: tuple[ResearchText, ...] = Field(default=(), max_length=16)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    fields: tuple[GuiResearchEvidenceField, ...] = Field(default=(), max_length=16)


class GuiResearchEvidenceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    items: tuple[GuiResearchEvidenceItem, ...] = Field(max_length=128)


class GuiKronosForecastView(BaseModel):
    """Browser-safe projection; raw/sample artifact references stay server-side."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["succeeded", "failed"]
    actual_model_inference: bool
    forecast_id: UUID | None = None
    model_id: ResearchText | None = None
    model_revision: ResearchText | None = None
    generated_at: UTCDateTime | None = None
    horizon_bars: int | None = Field(default=None, ge=1, le=256)
    path_count: int | None = Field(default=None, ge=1, le=32)
    expected_return: Decimal | None = None
    median_return: Decimal | None = None
    direction_probability: Decimal | None = Field(default=None, ge=0, le=1)
    expected_volatility: Decimal | None = Field(default=None, ge=0)
    downside_quantile: Decimal | None = None
    max_drawdown_quantile: Decimal | None = None
    quality_status: ResearchText | None = None
    warnings: tuple[ResearchText, ...] = Field(default=(), max_length=32)
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )

    @model_validator(mode="after")
    def validate_shape(self) -> GuiKronosForecastView:
        metrics = (
            self.forecast_id,
            self.model_id,
            self.model_revision,
            self.generated_at,
            self.horizon_bars,
            self.path_count,
            self.expected_return,
            self.median_return,
            self.direction_probability,
            self.expected_volatility,
            self.downside_quantile,
            self.max_drawdown_quantile,
            self.quality_status,
        )
        succeeded = (
            self.state == "succeeded"
            and self.actual_model_inference
            and all(value is not None for value in metrics)
            and self.error_code is None
        )
        failed = (
            self.state == "failed"
            and not self.actual_model_inference
            and all(value is None for value in metrics)
            and self.error_code is not None
            and not self.warnings
        )
        if not (succeeded or failed):
            raise ValueError("GUI Kronos forecast projection is inconsistent")
        return self


class GuiKronosAlphaView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["blocked", "mapped"]
    deployment_state: Literal["shadow", "paper_eligible"]
    eligible: bool
    weight: Decimal = Field(ge=0, le=1)
    reason_codes: tuple[ResearchText, ...] = Field(min_length=1, max_length=32)
    value: Decimal | None = Field(default=None, ge=-1, le=1)
    direction: Literal["long", "short", "neutral"] | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_authority(self) -> GuiKronosAlphaView:
        if self.state == "blocked":
            if (
                self.eligible
                or self.weight != 0
                or self.value is not None
                or self.direction is not None
                or self.confidence is not None
            ):
                raise ValueError("blocked GUI alpha must have zero authority")
            return self
        if self.value is None or self.direction is None or self.confidence is None:
            raise ValueError("mapped GUI alpha is incomplete")
        return self


class GuiResearchRunView(BaseModel):
    """Safe structured projection; raw job payloads never cross this boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    status: ResearchStatus
    stage: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    as_of: UTCDateTime | None = None
    snapshot_id: UUID | None = None
    evidence_count: int = Field(default=0, ge=0, le=100_000)
    claims: tuple[GuiResearchClaim, ...] = Field(default=(), max_length=64)
    counterarguments: tuple[ResearchText, ...] = Field(default=(), max_length=32)
    risks: tuple[ResearchText, ...] = Field(default=(), max_length=32)
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    kronos_forecast: GuiKronosForecastView | None = None
    kronos_alpha: GuiKronosAlphaView | None = None
    usage: GuiResearchUsageView | None = None
    issues: tuple[GuiResearchIssueView, ...] = Field(default=(), max_length=256)
    warnings: tuple[ResearchText, ...] = Field(default=(), max_length=64)
    versions: tuple[GuiResearchVersionView, ...] = Field(default=(), max_length=64)
    paper_decision: ResearchText | None = None
    report_content: str | None = Field(default=None, min_length=1, max_length=131_072)
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )
    updated_at: UTCDateTime
