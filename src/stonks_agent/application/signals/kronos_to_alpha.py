"""Evaluated, deployment-state-bound Kronos forecast to alpha mapper."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
    ForecastOutputArtifact,
    SignalDirection,
    SignalSource,
)
from stonks_agent.domain.strategy import (
    PromotionState,
    StrategyKind,
    StrategyManifest,
    StrategyRegistryEntry,
)
from stonks_contracts.common import (
    ConfidenceCalibration,
    NonNegativeDecimal,
    Sha256,
    SignedUnitDecimal,
    UnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)
from stonks_contracts.market_data import DataQualityStatus


class KronosFeatureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_artifact_schema: Literal["kronos-sample-paths/1.0.0"]
    predicted_return_field: Literal["expected_return"]
    probability_field: Literal["direction_probability"]
    interval: Literal["1d"]
    horizon_bars: int = Field(ge=1, le=256)


class KronosLabelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Literal["close_to_close_return"]
    benchmark: Literal["historical_market_benchmark"]
    horizon_bars: int = Field(ge=1, le=256)
    publication_lag: Literal["proven"]


class KronosUniverseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    markets: tuple[Literal["US", "HK", "TW"], ...] = Field(min_length=1, max_length=3)
    historical_membership_required: Literal[True] = True

    @model_validator(mode="after")
    def validate_markets(self) -> Self:
        if len(self.markets) != len(set(self.markets)):
            raise ValueError("Kronos universe markets must be unique")
        return self


class KronosCostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fee_bps: NonNegativeDecimal
    slippage_bps: NonNegativeDecimal
    cost_multipliers: tuple[Decimal, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_costs(self) -> Self:
        if (
            tuple(sorted(set(self.cost_multipliers))) != self.cost_multipliers
            or Decimal(1) not in self.cost_multipliers
            or any(value <= 0 for value in self.cost_multipliers)
        ):
            raise ValueError("Kronos cost multipliers are invalid")
        return self


class KronosMappingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_absolute_alpha: SignedUnitDecimal
    stale_minutes: int = Field(ge=1, le=10_080)
    expires_minutes: int = Field(ge=2, le=43_200)

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        if self.max_absolute_alpha <= 0:
            raise ValueError("Kronos alpha bound must be positive")
        if self.expires_minutes <= self.stale_minutes:
            raise ValueError("Kronos alpha expiry must follow stale time")
        return self

    @property
    def policy_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


class KronosStrategyConfiguration(BaseModel):
    """Closed strategy identity and current shadow deployment authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    manifest: StrategyManifest
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    model_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    model_artifact_hash: Sha256
    tokenizer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    tokenizer_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    tokenizer_artifact_hash: Sha256
    evaluation_policy_hash: Sha256
    required_baselines: tuple[str, ...] = Field(min_length=1, max_length=64)
    required_markets: tuple[Literal["US", "HK", "TW"], ...] = Field(
        min_length=1, max_length=3
    )
    deployment_state: Literal[PromotionState.SHADOW, PromotionState.PAPER_ELIGIBLE]
    paper_weight: UnitDecimal
    feature_spec: KronosFeatureSpec
    label_spec: KronosLabelSpec
    universe_spec: KronosUniverseSpec
    cost_spec: KronosCostSpec
    mapping: KronosMappingPolicy

    @model_validator(mode="after")
    def validate_strategy_binding(self) -> Self:
        baseline_ids_valid = len(self.required_baselines) == len(
            set(self.required_baselines)
        ) and all(value.strip() for value in self.required_baselines)
        hashes_match = (
            self.manifest.feature_spec_hash
            == stable_payload_hash(self.feature_spec.model_dump(mode="json"))
            and self.manifest.label_spec_hash
            == stable_payload_hash(self.label_spec.model_dump(mode="json"))
            and self.manifest.universe_spec_hash
            == stable_payload_hash(self.universe_spec.model_dump(mode="json"))
            and self.manifest.cost_model_hash
            == stable_payload_hash(self.cost_spec.model_dump(mode="json"))
            and self.manifest.split_policy_hash == self.evaluation_policy_hash
            and self.manifest.parameters_hash == self.mapping.policy_hash
        )
        identity_matches = (
            self.manifest.kind is StrategyKind.FORECAST_MAPPER
            and not self.manifest.deterministic
            and self.required_markets == self.universe_spec.markets
            and self.feature_spec.horizon_bars == self.label_spec.horizon_bars
        )
        shadow_is_zero = (
            self.deployment_state is not PromotionState.SHADOW or self.paper_weight == 0
        )
        if not (
            baseline_ids_valid and hashes_match and identity_matches and shadow_is_zero
        ):
            raise ValueError("Kronos strategy configuration binding is invalid")
        return self

    @property
    def configuration_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


class KronosToAlphaCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    forecast_output: ForecastOutputArtifact
    registry: StrategyRegistryEntry
    evaluation: EvaluationReport
    generated_at: UTCDateTime

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.generated_at < self.forecast_output.created_at:
            raise ValueError("Kronos alpha cannot precede forecast archival")
        return self


def load_kronos_strategy_configuration(
    path: str | Path,
) -> KronosStrategyConfiguration:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return KronosStrategyConfiguration.model_validate(payload)
    except (OSError, TypeError, ValidationError, yaml.YAMLError) as error:
        raise ValueError("Kronos strategy configuration could not be loaded") from error


def map_kronos_to_alpha(
    command: KronosToAlphaCommand,
    configuration: KronosStrategyConfiguration,
) -> Result[AlphaSignal]:
    binding_error = _binding_error(command, configuration)
    if binding_error is not None:
        return _failure(ErrorCode.CONFLICT, binding_error)
    forecast = command.forecast_output.forecast
    value = _bounded_value(
        forecast.expected_return, configuration.mapping.max_absolute_alpha
    )
    direction = _direction(value)
    confidence = abs(forecast.direction_probability - Decimal("0.5")) * 2
    return Success(
        AlphaSignal(
            signal_id=uuid5(
                NAMESPACE_URL,
                stable_payload_hash(
                    {
                        "forecast_id": str(forecast.forecast_id),
                        "configuration_hash": configuration.configuration_hash,
                        "evaluation_hash": command.evaluation.evaluation_hash,
                        "generated_at": command.generated_at.isoformat(),
                    }
                ),
            ),
            strategy_id=configuration.manifest.strategy_id,
            strategy_version=configuration.manifest.strategy_version,
            instrument_id=forecast.instrument_id,
            as_of=forecast.as_of,
            generated_at=command.generated_at,
            stale_at=command.generated_at
            + timedelta(minutes=configuration.mapping.stale_minutes),
            expires_at=command.generated_at
            + timedelta(minutes=configuration.mapping.expires_minutes),
            horizon=f"{forecast.horizon_bars}{forecast.interval}",
            value=value,
            confidence=confidence,
            calibration=command.evaluation.calibration,
            direction=direction,
            source=SignalSource.FORECAST,
            strategy_manifest_hash=configuration.manifest.manifest_hash,
            dataset_snapshot_id=forecast.dataset_snapshot_id,
            data_hash=command.forecast_output.data_hash,
            runtime_hash=command.forecast_output.runtime_hash,
            evaluation_policy_hash=command.evaluation.evaluation_policy_hash,
            raw_output_artifact_ref=command.forecast_output.raw_output_artifact_ref,
            evaluation_report_id=command.evaluation.report_id,
            evaluation_hash=command.evaluation.evaluation_hash,
            forecast_refs=(forecast.forecast_id,),
            reason_codes=(
                "kronos_archived_forecast",
                f"deployment:{configuration.deployment_state.value}",
            ),
        )
    )


def _binding_error(
    command: KronosToAlphaCommand,
    configuration: KronosStrategyConfiguration,
) -> str | None:
    output = command.forecast_output
    registry = command.registry
    manifest = registry.manifest
    evaluation = command.evaluation
    if registry.state is not configuration.deployment_state:
        return "Kronos registry state does not match deployment configuration"
    if manifest != configuration.manifest:
        return "Kronos registry manifest does not match configuration"
    if not evaluation.passed or evaluation.valid_until <= command.generated_at:
        return "Kronos evaluation is not valid"
    if evaluation.calibration is not ConfidenceCalibration.CALIBRATED:
        return "Kronos evaluation is not calibrated"
    if not _evaluation_matches(registry, evaluation):
        return "Kronos evaluation binding mismatch"
    if not _forecast_matches(output, configuration):
        return "Kronos forecast runtime or model binding mismatch"
    return None


def _evaluation_matches(
    registry: StrategyRegistryEntry,
    evaluation: EvaluationReport,
) -> bool:
    manifest = registry.manifest
    return (
        evaluation.strategy_id == manifest.strategy_id
        and evaluation.strategy_version == manifest.strategy_version
        and evaluation.strategy_manifest_hash == manifest.manifest_hash
        and evaluation.runtime_hash == manifest.runtime_hash
        and registry.evaluation_report_id == evaluation.report_id
        and registry.evaluation_hash == evaluation.evaluation_hash
    )


def _forecast_matches(
    output: ForecastOutputArtifact,
    configuration: KronosStrategyConfiguration,
) -> bool:
    forecast = output.forecast
    return (
        forecast.model_id == configuration.model_id
        and forecast.model_revision == configuration.model_revision
        and output.model_artifact_hash == configuration.model_artifact_hash
        and forecast.tokenizer_id == configuration.tokenizer_id
        and forecast.tokenizer_revision == configuration.tokenizer_revision
        and output.tokenizer_artifact_hash == configuration.tokenizer_artifact_hash
        and output.runtime_hash == configuration.manifest.runtime_hash
        and forecast.interval == configuration.feature_spec.interval
        and forecast.horizon_bars == configuration.feature_spec.horizon_bars
        and forecast.input_quality.status
        in {
            DataQualityStatus.AVAILABLE,
            DataQualityStatus.ESTIMATED,
            DataQualityStatus.PARTIAL,
        }
        and forecast.input_quality.completeness > 0
    )


def _bounded_value(value: Decimal, bound: Decimal) -> Decimal:
    return max(-bound, min(value, bound))


def _direction(value: Decimal) -> SignalDirection:
    if value > 0:
        return SignalDirection.LONG
    if value < 0:
        return SignalDirection.SHORT
    return SignalDirection.NEUTRAL


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
