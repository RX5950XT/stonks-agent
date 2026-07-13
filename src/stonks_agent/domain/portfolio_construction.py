"""Immutable inputs and policy for deterministic portfolio construction."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from stonks_agent.domain._trading import TradingModel
from stonks_agent.domain.evaluation import EvaluationReport
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot
from stonks_agent.domain.signal import AlphaSignal
from stonks_agent.domain.strategy import StrategyRegistryEntry
from stonks_contracts.common import (
    Currency,
    NonNegativeDecimal,
    PositiveDecimal,
    UnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)


class PortfolioStrategyWeight(TradingModel):
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    strategy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    weight: UnitDecimal

    @field_validator("weight")
    @classmethod
    def require_positive_weight(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("portfolio strategy weight must be positive")
        return value

    @property
    def key(self) -> tuple[str, str]:
        return self.strategy_id, self.strategy_version


class PortfolioPolicy(TradingModel):
    policy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    strategy_weights: tuple[PortfolioStrategyWeight, ...] = Field(
        min_length=1,
        max_length=256,
    )
    deadband: UnitDecimal
    shrinkage: UnitDecimal
    turnover_penalty: UnitDecimal
    max_position_weight: UnitDecimal
    estimated_cost_bps: NonNegativeDecimal
    currency_quantum: PositiveDecimal
    long_only: Literal[True]

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        keys = tuple(item.key for item in self.strategy_weights)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("portfolio strategy weights must be unique and sorted")
        if sum((item.weight for item in self.strategy_weights), Decimal(0)) != 1:
            raise ValueError("portfolio strategy weight sum must equal one")
        if self.shrinkage <= 0 or self.max_position_weight <= 0:
            raise ValueError("shrinkage and position bound must be positive")
        if self.estimated_cost_bps > Decimal("10000"):
            raise ValueError("estimated cost bps cannot exceed 10000")
        return self

    @property
    def policy_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


class PortfolioMark(TradingModel):
    instrument_id: UUID
    as_of: UTCDateTime
    currency: Currency
    price: PositiveDecimal
    quantity_quantum: PositiveDecimal


class PortfolioSignalCandidate(TradingModel):
    signal: AlphaSignal
    registry: StrategyRegistryEntry
    evaluation: EvaluationReport


class BuildTargetCommand(TradingModel):
    target_id: UUID
    snapshot: AccountPortfolioSnapshot
    base_currency: Currency
    marks: tuple[PortfolioMark, ...] = Field(max_length=100_000)
    signal_candidates: tuple[PortfolioSignalCandidate, ...] = Field(max_length=100_000)
