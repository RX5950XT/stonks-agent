"""Frozen point-in-time inputs and policy for the hard risk gate."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel
from stonks_agent.domain.calendar import MarketSession
from stonks_agent.domain.ledger import LedgerHead
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot, PortfolioTarget
from stonks_agent.domain.portfolio_construction import PortfolioSignalCandidate
from stonks_agent.domain.reservations import AccountReservation
from stonks_contracts.common import (
    Currency,
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    UnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)
from stonks_contracts.instrument import AssetClass


class HardRiskPolicy(TradingModel):
    policy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    decision_valid_seconds: int = Field(ge=1, le=3600)
    max_data_age_seconds: int = Field(ge=0, le=86_400)
    max_kill_switch_age_seconds: int = Field(ge=0, le=3600)
    allowed_asset_classes: tuple[AssetClass, ...] = Field(min_length=1)
    max_pending_orders: int = Field(ge=0, le=100_000)
    max_single_position_weight: UnitDecimal
    max_sector_weight: UnitDecimal
    max_asset_class_weight: UnitDecimal
    max_gross_exposure: NonNegativeDecimal
    min_net_exposure: NonNegativeDecimal
    max_net_exposure: NonNegativeDecimal
    max_turnover: NonNegativeDecimal
    max_adv_participation: UnitDecimal
    max_drawdown: UnitDecimal
    max_daily_loss: UnitDecimal
    require_market_open: bool

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        values = tuple(item.value for item in self.allowed_asset_classes)
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("allowed asset classes must be unique and sorted")
        positive = (
            self.max_single_position_weight,
            self.max_sector_weight,
            self.max_asset_class_weight,
            self.max_gross_exposure,
            self.max_net_exposure,
            self.max_turnover,
            self.max_adv_participation,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("hard risk upper limits must be positive")
        if self.min_net_exposure > self.max_net_exposure:
            raise ValueError("minimum net exposure cannot exceed maximum")
        return self

    @property
    def policy_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


class RiskInstrumentState(TradingModel):
    instrument_id: UUID
    asset_class: AssetClass
    sector: NonEmptyString
    mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    currency: Currency
    mark_price: PositiveDecimal
    mark_as_of: UTCDateTime
    quantity_quantum: PositiveDecimal
    average_daily_volume: PositiveDecimal
    session: MarketSession

    @model_validator(mode="after")
    def validate_session(self) -> Self:
        if self.session.mic != self.mic:
            raise ValueError("risk instrument and market session MIC differ")
        return self


class RiskKillSwitchState(TradingModel):
    global_active: bool
    account_active: bool
    observed_at: UTCDateTime


class BuildRiskDecisionCommand(TradingModel):
    decision_id: UUID
    snapshot: AccountPortfolioSnapshot
    target: PortfolioTarget
    instruments: tuple[RiskInstrumentState, ...] = Field(max_length=100_000)
    signal_candidates: tuple[PortfolioSignalCandidate, ...] = Field(max_length=100_000)
    open_reservations: tuple[AccountReservation, ...] = Field(max_length=100_000)
    ledger_head: LedgerHead
    high_watermark_nav: PositiveDecimal
    day_start_nav: PositiveDecimal
    kill_switch: RiskKillSwitchState
    at: UTCDateTime
