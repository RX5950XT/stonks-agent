"""Browser-safe typed paper account projections."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from stonks_contracts.common import UTCDateTime


class GuiPaperCashView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,11}$")
    settled: Decimal = Field(ge=0)
    reserved: Decimal = Field(ge=0)
    available: Decimal = Field(ge=0)


class GuiPaperPositionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: UUID
    quantity: Decimal = Field(ge=0)
    sellable: Decimal = Field(ge=0)
    reserved: Decimal = Field(ge=0)
    available: Decimal = Field(ge=0)


class GuiPaperPortfolioView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_currency: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,11}$")
    as_of: UTCDateTime
    cash: tuple[GuiPaperCashView, ...] = Field(max_length=64)
    positions: tuple[GuiPaperPositionView, ...] = Field(max_length=128)
    position_count: int = Field(ge=0, le=100_000)
    pending_order_count: int = Field(ge=0, le=100_000)
    latest_target: bool


class GuiPaperNavView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["available", "empty", "unavailable"]
    as_of: UTCDateTime | None = None
    base_currency: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9]{1,11}$",
    )
    nav: Decimal | None = Field(default=None, ge=0)
    cash_value: Decimal | None = Field(default=None, ge=0)
    position_value: Decimal | None = Field(default=None, ge=0)
    cumulative_fees: Decimal | None = Field(default=None, ge=0)
    realized_pnl: Decimal | None = None
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )


class GuiPaperRiskView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["available", "empty", "unavailable"]
    approved: bool | None = None
    currently_authorized: bool | None = None
    failed_checks: tuple[str, ...] = Field(default=(), max_length=64)
    policy_version: str | None = Field(default=None, max_length=128)
    decided_at: UTCDateTime | None = None
    expires_at: UTCDateTime | None = None
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )


class GuiPaperIntegrityView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["verified"]
    account_sequence: int = Field(ge=0)
    portfolio_sequence: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    ledger_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    projection_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class GuiPaperSafetyView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["available", "unavailable"]
    active: bool | None = None
    reason_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{0,127}$",
    )
    version: int | None = Field(default=None, ge=1)
    updated_at: UTCDateTime | None = None
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )
