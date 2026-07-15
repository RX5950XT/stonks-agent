"""Immutable, authority-free evidence for cross-engine backtest parity."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_contracts.backtest import BacktestEngineKind, BacktestRuntimeIdentity
from stonks_contracts.common import Sha256, UTCDateTime, stable_payload_hash

_PLACEHOLDER_HASH = "0" * 64
REQUIRED_PARITY_ENGINES = tuple(BacktestEngineKind)


class EngineParityStatus(StrEnum):
    CANONICAL_PARITY = "canonical_parity"
    ENGINE_SPECIFIC = "engine_specific"


class EngineParityDimension(StrEnum):
    SEMANTIC_HASH = "semantic_hash"
    ORDER_OUTCOMES = "order_outcomes"
    FILL_SCHEDULE = "fill_schedule"
    FILL_QUANTITY = "fill_quantity"
    FILL_PRICE = "fill_price"
    FILL_FEES = "fill_fees"
    FILL_SLIPPAGE = "fill_slippage"
    FINAL_CASH = "final_cash"
    FINAL_POSITIONS = "final_positions"
    TOTAL_FEES = "total_fees"
    WARNINGS = "warnings"


class EngineResultEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: BacktestEngineKind
    runtime: BacktestRuntimeIdentity
    job_hash: Sha256
    result_hash: Sha256
    semantic_hash: Sha256
    fill_count: int = Field(ge=0, le=2_000_000)
    fill_provenance_hash: Sha256
    warning_count: int = Field(ge=0, le=128)
    warnings_hash: Sha256

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if self.runtime.engine is not self.engine:
            raise ValueError("parity evidence runtime engine changed")
        return self


class EngineParityComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_engine: BacktestEngineKind
    dimension: EngineParityDimension
    difference_count: int = Field(ge=0, le=2_000_000)
    threshold: int = Field(ge=0, le=128)
    within_threshold: bool
    evidence_keys: tuple[Sha256, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_threshold_and_keys(self) -> Self:
        if self.candidate_engine is BacktestEngineKind.REFERENCE:
            raise ValueError("reference engine cannot be its own parity candidate")
        if self.within_threshold != (self.difference_count <= self.threshold):
            raise ValueError("parity comparison threshold decision changed")
        if self.evidence_keys != tuple(sorted(set(self.evidence_keys))):
            raise ValueError("parity evidence keys must be unique and stably ordered")
        return self


class EngineParityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: UUID
    claim_scope: Literal["fixture_canonical_semantics_only"] = (
        "fixture_canonical_semantics_only"
    )
    normalization_scope: Literal["adapter_normalized_not_native_matching"] = (
        "adapter_normalized_not_native_matching"
    )
    input_hash: Sha256
    dataset_hash: Sha256
    calendar_hash: Sha256
    cost_model_hash: Sha256
    policy_hash: Sha256
    evidence: tuple[EngineResultEvidence, ...] = Field(min_length=3, max_length=3)
    comparisons: tuple[EngineParityComparison, ...] = Field(
        min_length=1,
        max_length=64,
    )
    status: EngineParityStatus
    evaluated_at: UTCDateTime
    parity_hash: Sha256

    @classmethod
    def create(cls, **values: object) -> EngineParityReport:
        draft = cls.model_construct(
            **values,  # type: ignore[arg-type]
            parity_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"parity_hash": draft.expected_parity_hash()}
        )

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        engines = tuple(item.engine for item in self.evidence)
        if engines != REQUIRED_PARITY_ENGINES:
            raise ValueError("parity evidence must use the exact stable engine order")
        keys = tuple(
            (item.candidate_engine.value, item.dimension.value)
            for item in self.comparisons
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("parity comparisons must be unique and stably ordered")
        expected = (
            EngineParityStatus.CANONICAL_PARITY
            if all(item.within_threshold for item in self.comparisons)
            else EngineParityStatus.ENGINE_SPECIFIC
        )
        if self.status is not expected:
            raise ValueError("parity status does not match comparisons")
        if self.parity_hash != self.expected_parity_hash():
            raise ValueError("parity hash mismatch")
        return self

    def expected_parity_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(
                mode="json",
                exclude={"evaluation_id", "evaluated_at", "parity_hash"},
            )
        )
