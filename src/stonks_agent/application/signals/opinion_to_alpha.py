"""Default-disabled, evaluated AgentOpinion to AlphaSignal mapper."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Self
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evaluation import EvaluationReport
from stonks_agent.domain.signal import (
    AlphaSignal,
    SignalDirection,
    SignalSource,
)
from stonks_agent.domain.strategy import (
    PromotionState,
    StrategyKind,
    StrategyRegistryEntry,
)
from stonks_contracts.common import (
    ArtifactRef,
    ConfidenceCalibration,
    Sha256,
    SignedUnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)
from stonks_contracts.research import AgentOpinion


class OpinionToAlphaPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    strategy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    bullish_value: SignedUnitDecimal
    neutral_value: SignedUnitDecimal
    bearish_value: SignedUnitDecimal
    stale_minutes: int = Field(ge=1, le=10_080)
    expires_minutes: int = Field(ge=2, le=43_200)

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        if not (
            self.bullish_value > 0
            and self.neutral_value == 0
            and self.bearish_value < 0
        ):
            raise ValueError("opinion mapper values must preserve fixed direction")
        if self.expires_minutes <= self.stale_minutes:
            raise ValueError("opinion mapper expiry must be later than stale time")
        return self

    @property
    def policy_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


class OpinionToAlphaCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opinion: AgentOpinion
    registry: StrategyRegistryEntry
    evaluation: EvaluationReport
    dataset_snapshot_id: UUID
    data_hash: Sha256
    raw_output_artifact_ref: ArtifactRef
    generated_at: UTCDateTime

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.generated_at < self.opinion.as_of:
            raise ValueError("alpha generation cannot precede opinion as_of")
        return self


def load_opinion_mapper_policy(path: str | Path) -> OpinionToAlphaPolicy:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return OpinionToAlphaPolicy.model_validate(payload)
    except (OSError, yaml.YAMLError, TypeError) as error:
        raise ValueError("opinion mapper policy could not be loaded") from error


def map_opinion_to_alpha(
    command: OpinionToAlphaCommand,
    policy: OpinionToAlphaPolicy,
) -> Result[AlphaSignal]:
    if not policy.enabled:
        return _failure(
            ErrorCode.CAPABILITY_DENIED,
            "Opinion-to-alpha mapper is disabled",
        )
    mapping = {
        "bullish": (policy.bullish_value, SignalDirection.LONG),
        "neutral": (policy.neutral_value, SignalDirection.NEUTRAL),
        "bearish": (policy.bearish_value, SignalDirection.SHORT),
    }
    mapped = mapping.get(command.opinion.recommendation)
    if mapped is None:
        return _failure(ErrorCode.INVALID_INPUT, "Opinion recommendation is not mapped")
    if command.opinion.calibration is not ConfidenceCalibration.CALIBRATED:
        return _failure(ErrorCode.INVALID_INPUT, "Opinion confidence is uncalibrated")
    binding_error = _binding_error(command, policy)
    if binding_error is not None:
        return _failure(ErrorCode.CONFLICT, binding_error)
    value, direction = mapped
    signal_id = uuid5(NAMESPACE_URL, _signal_identity(command, policy, value))
    return Success(
        AlphaSignal(
            signal_id=signal_id,
            strategy_id=policy.strategy_id,
            strategy_version=policy.strategy_version,
            instrument_id=command.opinion.instrument_id,
            as_of=command.opinion.as_of,
            generated_at=command.generated_at,
            stale_at=command.generated_at + timedelta(minutes=policy.stale_minutes),
            expires_at=command.generated_at + timedelta(minutes=policy.expires_minutes),
            horizon=command.opinion.horizon,
            value=value,
            confidence=command.opinion.confidence,
            calibration=command.opinion.calibration,
            direction=direction,
            source=SignalSource.OPINION,
            strategy_manifest_hash=command.registry.manifest.manifest_hash,
            dataset_snapshot_id=command.dataset_snapshot_id,
            data_hash=command.data_hash,
            runtime_hash=command.registry.manifest.runtime_hash,
            evaluation_policy_hash=command.evaluation.evaluation_policy_hash,
            raw_output_artifact_ref=command.raw_output_artifact_ref,
            evaluation_report_id=command.evaluation.report_id,
            evaluation_hash=command.evaluation.evaluation_hash,
            evidence_refs=command.opinion.evidence_refs,
            reason_codes=(
                f"opinion:{command.opinion.recommendation}",
                f"mapper_policy:{policy.policy_hash}",
            ),
        )
    )


def _binding_error(
    command: OpinionToAlphaCommand,
    policy: OpinionToAlphaPolicy,
) -> str | None:
    registry = command.registry
    manifest = registry.manifest
    evaluation = command.evaluation
    if registry.state is not PromotionState.PAPER_ELIGIBLE:
        return "Opinion mapper strategy is not paper eligible"
    if manifest.kind is not StrategyKind.OPINION_MAPPER:
        return "Registered strategy is not an opinion mapper"
    if (
        manifest.strategy_id != policy.strategy_id
        or manifest.strategy_version != policy.strategy_version
        or manifest.parameters_hash != policy.policy_hash
    ):
        return "Opinion mapper policy does not match registered manifest"
    if not evaluation.passed or evaluation.valid_until <= command.generated_at:
        return "Opinion mapper evaluation is not valid"
    if (
        evaluation.strategy_id != manifest.strategy_id
        or evaluation.strategy_version != manifest.strategy_version
        or evaluation.strategy_manifest_hash != manifest.manifest_hash
        or evaluation.runtime_hash != manifest.runtime_hash
        or registry.evaluation_report_id != evaluation.report_id
        or registry.evaluation_hash != evaluation.evaluation_hash
    ):
        return "Opinion mapper evaluation binding mismatch"
    return None


def _signal_identity(
    command: OpinionToAlphaCommand,
    policy: OpinionToAlphaPolicy,
    value: Decimal,
) -> str:
    return stable_payload_hash(
        {
            "opinion_id": str(command.opinion.opinion_id),
            "strategy_id": policy.strategy_id,
            "strategy_version": policy.strategy_version,
            "policy_hash": policy.policy_hash,
            "evaluation_hash": command.evaluation.evaluation_hash,
            "dataset_snapshot_id": str(command.dataset_snapshot_id),
            "data_hash": command.data_hash,
            "generated_at": command.generated_at.isoformat(),
            "value": str(value),
        }
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
