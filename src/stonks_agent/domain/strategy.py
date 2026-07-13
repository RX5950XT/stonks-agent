"""Immutable strategy identity and paper-only promotion state."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_contracts.common import (
    ArtifactRef,
    NonEmptyString,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)


class StrategyKind(StrEnum):
    DETERMINISTIC = "deterministic"
    FORECAST_MAPPER = "forecast_mapper"
    OPINION_MAPPER = "opinion_mapper"
    QUANT_MODEL = "quant_model"


class PromotionState(StrEnum):
    DRAFT = "draft"
    EVALUATING = "evaluating"
    REJECTED = "rejected"
    SHADOW = "shadow"
    PAPER_ELIGIBLE = "paper_eligible"
    SUSPENDED = "suspended"
    RETIRED = "retired"


_ALLOWED_TRANSITIONS: dict[PromotionState, frozenset[PromotionState]] = {
    PromotionState.DRAFT: frozenset({PromotionState.EVALUATING}),
    PromotionState.EVALUATING: frozenset(
        {PromotionState.REJECTED, PromotionState.SHADOW}
    ),
    PromotionState.REJECTED: frozenset(),
    PromotionState.SHADOW: frozenset({PromotionState.PAPER_ELIGIBLE}),
    PromotionState.PAPER_ELIGIBLE: frozenset(
        {PromotionState.SUSPENDED, PromotionState.RETIRED}
    ),
    PromotionState.SUSPENDED: frozenset(
        {PromotionState.EVALUATING, PromotionState.RETIRED}
    ),
    PromotionState.RETIRED: frozenset(),
}


class StrategyManifest(BaseModel):
    """Content-addressed identity for code, runtime, data contracts, and policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_id: UUID
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    strategy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    kind: StrategyKind
    source_artifact_ref: ArtifactRef
    runtime_hash: Sha256
    feature_spec_hash: Sha256
    label_spec_hash: Sha256
    universe_spec_hash: Sha256
    cost_model_hash: Sha256
    split_policy_hash: Sha256
    parameters_hash: Sha256
    owner: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    deterministic: bool
    created_at: UTCDateTime

    @property
    def manifest_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(
                mode="json",
                exclude={"manifest_id", "created_at"},
            )
        )


class StrategyRegistryEntry(BaseModel):
    """Versioned registry projection; P3.2 persists it with CAS and audit events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: StrategyManifest
    state: PromotionState
    evaluation_report_id: UUID | None = None
    evaluation_hash: Sha256 | None = None
    version: int = Field(ge=1)
    created_at: UTCDateTime
    updated_at: UTCDateTime

    @model_validator(mode="after")
    def validate_evaluation_binding_and_timeline(self) -> Self:
        has_report = self.evaluation_report_id is not None
        if has_report != (self.evaluation_hash is not None):
            raise ValueError("evaluation report id and hash must be bound together")
        evaluated_states = {
            PromotionState.REJECTED,
            PromotionState.SHADOW,
            PromotionState.PAPER_ELIGIBLE,
            PromotionState.SUSPENDED,
            PromotionState.RETIRED,
        }
        if self.state in evaluated_states and not has_report:
            raise ValueError(
                "post-evaluation strategy state requires evaluation binding"
            )
        if self.updated_at < self.created_at:
            raise ValueError("strategy update cannot precede creation")
        return self


class StrategyTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: NonEmptyString
    strategy_version: NonEmptyString
    expected_version: int = Field(ge=1)
    current_state: PromotionState
    target_state: PromotionState
    evaluation_report_id: UUID | None = None
    evaluation_hash: Sha256 | None = None
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    actor: str = Field(pattern=r"^[a-z][a-z0-9_.:@-]{0,127}$")
    requested_at: UTCDateTime

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        if not can_transition(self.current_state, self.target_state):
            raise ValueError("strategy promotion transition is not allowed")
        has_report = self.evaluation_report_id is not None
        if has_report != (self.evaluation_hash is not None):
            raise ValueError("evaluation report id and hash must be bound together")
        if (
            self.target_state
            in {
                PromotionState.REJECTED,
                PromotionState.SHADOW,
                PromotionState.PAPER_ELIGIBLE,
            }
            and not has_report
        ):
            raise ValueError("evaluation-gated transition requires evaluation binding")
        return self


class StrategyAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    strategy_id: NonEmptyString
    strategy_version: NonEmptyString
    sequence: int = Field(ge=1)
    event_type: str = Field(pattern=r"^strategy\.[a-z_]{1,63}$")
    from_state: PromotionState | None
    to_state: PromotionState
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    actor: str = Field(pattern=r"^[a-z][a-z0-9_.:@-]{0,127}$")
    evaluation_report_id: UUID | None = None
    evaluation_hash: Sha256 | None = None
    occurred_at: UTCDateTime
    previous_hash: Sha256 | None = None
    event_hash: Sha256

    @model_validator(mode="after")
    def validate_chain_shape(self) -> Self:
        if (self.sequence == 1) != (self.previous_hash is None):
            raise ValueError("only the genesis strategy event may omit previous hash")
        has_report = self.evaluation_report_id is not None
        if has_report != (self.evaluation_hash is not None):
            raise ValueError("audit evaluation id and hash must be bound together")
        return self


class StrategyMutationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: StrategyRegistryEntry
    event: StrategyAuditEvent


def can_transition(current: PromotionState, target: PromotionState) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]
