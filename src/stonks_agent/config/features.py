"""Typed, default-off catalog for optional integrations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.domain.errors import ErrorCode, StructuredError


class IntegrationName(StrEnum):
    AI_TRADER = "ai_trader"
    OPENBB = "openbb"
    TRADINGAGENTS = "tradingagents"
    KRONOS = "kronos"
    QLIB = "qlib"
    NAUTILUS = "nautilus"
    LEAN = "lean"
    RD_AGENT = "rd_agent"
    FREQTRADE = "freqtrade"
    FINRL = "finrl"
    VECTORBT = "vectorbt"


class IntegrationKind(StrEnum):
    EXTERNAL_HTTP = "external_http"
    SIDECAR = "sidecar"
    WORKER = "worker"
    SANDBOX = "sandbox"
    FUTURE_RFC = "future_rfc"


class NetworkPolicy(StrEnum):
    EXTERNAL_HTTPS = "external_https"
    PROVIDER_EGRESS = "provider_egress"
    INTERNAL = "internal"
    NONE = "none"


class OutputScope(StrEnum):
    UNTRUSTED_EVIDENCE = "untrusted_evidence"
    CANONICAL_OBSERVATION = "canonical_observation"
    RESEARCH_ARTIFACT = "research_artifact"
    FORECAST_ONLY = "forecast_only"
    EVALUATION_ONLY = "evaluation_only"
    NONE = "none"


DEPLOYABLE_INTEGRATIONS = (
    IntegrationName.OPENBB,
    IntegrationName.TRADINGAGENTS,
    IntegrationName.KRONOS,
    IntegrationName.QLIB,
    IntegrationName.NAUTILUS,
    IntegrationName.LEAN,
    IntegrationName.RD_AGENT,
)
FUTURE_RFC_INTEGRATIONS = (
    IntegrationName.FREQTRADE,
    IntegrationName.FINRL,
    IntegrationName.VECTORBT,
)

_SERVICE_OIDC_TRUST_ENVIRONMENT = (
    "STONKS_SERVICE_OIDC_ISSUER",
    "STONKS_SERVICE_OIDC_CORE_SUBJECT",
    "STONKS_SERVICE_OIDC_CORE_CLIENT_ID",
    "STONKS_SERVICE_OIDC_JWKS_HOST_FILE",
)


def _service_oidc_environment(audience: str) -> tuple[str, ...]:
    return (*_SERVICE_OIDC_TRUST_ENVIRONMENT, audience)


@dataclass(frozen=True, slots=True)
class IntegrationBoundary:
    kind: IntegrationKind
    compose_profiles: tuple[str, ...]
    config_paths: tuple[str, ...]
    required_environment: tuple[str, ...]
    network_policy: NetworkPolicy
    output_scope: OutputScope


INTEGRATION_BOUNDARIES: Final[Mapping[IntegrationName, IntegrationBoundary]] = (
    MappingProxyType(
        {
            IntegrationName.AI_TRADER: IntegrationBoundary(
                IntegrationKind.EXTERNAL_HTTP,
                (),
                ("config/platforms/ai_trader.yaml",),
                ("AI_TRADER_ACCESS_TOKEN",),
                NetworkPolicy.EXTERNAL_HTTPS,
                OutputScope.UNTRUSTED_EVIDENCE,
            ),
            IntegrationName.OPENBB: IntegrationBoundary(
                IntegrationKind.SIDECAR,
                ("openbb",),
                (
                    "sidecars/openbb/provider-manifest.yaml",
                    "sidecars/openbb/license-policy.yaml",
                ),
                (),
                NetworkPolicy.PROVIDER_EGRESS,
                OutputScope.CANONICAL_OBSERVATION,
            ),
            IntegrationName.TRADINGAGENTS: IntegrationBoundary(
                IntegrationKind.WORKER,
                (
                    "tradingagents-paper",
                    "tradingagents-backtest",
                    "tradingagents-production",
                ),
                ("config/workers/tradingagents.yaml",),
                _service_oidc_environment("STONKS_TRADINGAGENTS_SERVICE_OIDC_AUDIENCE"),
                NetworkPolicy.INTERNAL,
                OutputScope.RESEARCH_ARTIFACT,
            ),
            IntegrationName.KRONOS: IntegrationBoundary(
                IntegrationKind.WORKER,
                ("kronos-cpu", "kronos-cuda"),
                (
                    "config/workers/kronos_cpu.yaml",
                    "config/workers/kronos_cuda.yaml",
                ),
                (
                    "STONKS_KRONOS_MODEL_ROOT",
                    *_service_oidc_environment("STONKS_KRONOS_SERVICE_OIDC_AUDIENCE"),
                ),
                NetworkPolicy.INTERNAL,
                OutputScope.FORECAST_ONLY,
            ),
            IntegrationName.QLIB: IntegrationBoundary(
                IntegrationKind.WORKER,
                ("qlib",),
                (),
                _service_oidc_environment("STONKS_QUANT_LAB_SERVICE_OIDC_AUDIENCE"),
                NetworkPolicy.INTERNAL,
                OutputScope.EVALUATION_ONLY,
            ),
            IntegrationName.NAUTILUS: IntegrationBoundary(
                IntegrationKind.SIDECAR,
                ("nautilus",),
                ("sidecars/nautilus/distribution-manifest.yaml",),
                (
                    "STONKS_NAUTILUS_RUNTIME_HASH",
                    "STONKS_NAUTILUS_IMAGE_DIGEST",
                    *_service_oidc_environment("STONKS_NAUTILUS_SERVICE_OIDC_AUDIENCE"),
                ),
                NetworkPolicy.INTERNAL,
                OutputScope.EVALUATION_ONLY,
            ),
            IntegrationName.LEAN: IntegrationBoundary(
                IntegrationKind.SIDECAR,
                ("lean",),
                ("sidecars/lean/distribution-manifest.yaml",),
                (
                    "STONKS_LEAN_RUNTIME_HASH",
                    "STONKS_LEAN_IMAGE_DIGEST",
                    *_service_oidc_environment("STONKS_LEAN_SERVICE_OIDC_AUDIENCE"),
                ),
                NetworkPolicy.INTERNAL,
                OutputScope.EVALUATION_ONLY,
            ),
            IntegrationName.RD_AGENT: IntegrationBoundary(
                IntegrationKind.SANDBOX,
                ("rd-agent",),
                (
                    "workers/quant_lab/rd_agent/distribution-manifest.yaml",
                    "workers/quant_lab/rd_agent/sandbox_policy.yaml",
                ),
                ("STONKS_RD_RUNTIME_HASH", "STONKS_RD_IMAGE_DIGEST"),
                NetworkPolicy.NONE,
                OutputScope.EVALUATION_ONLY,
            ),
            IntegrationName.FREQTRADE: IntegrationBoundary(
                IntegrationKind.FUTURE_RFC,
                (),
                (),
                (),
                NetworkPolicy.NONE,
                OutputScope.NONE,
            ),
            IntegrationName.FINRL: IntegrationBoundary(
                IntegrationKind.FUTURE_RFC,
                (),
                (),
                (),
                NetworkPolicy.NONE,
                OutputScope.NONE,
            ),
            IntegrationName.VECTORBT: IntegrationBoundary(
                IntegrationKind.FUTURE_RFC,
                (),
                (),
                (),
                NetworkPolicy.NONE,
                OutputScope.NONE,
            ),
        }
    )
)


class SupplyChainPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    images: tuple[str, ...] = Field(min_length=1, max_length=4)
    lock_paths: tuple[str, ...] = Field(min_length=1, max_length=4)
    notice_paths: tuple[str, ...] = Field(min_length=1, max_length=4)
    upstream_project: str = Field(min_length=1, max_length=128)
    source_identity: str = Field(min_length=1, max_length=256)
    license_expression: str = Field(min_length=1, max_length=64)
    sbom_mode: Literal["committed", "ci_generated"]
    sbom_ref: str = Field(min_length=1, max_length=256)
    cve_policy: Literal["fail_on_high"]
    core_dependency_allowed: Literal[False]

    @model_validator(mode="after")
    def validate_paths_and_uniqueness(self) -> Self:
        values = self.images + self.lock_paths + self.notice_paths
        if any(not value.strip() for value in values):
            raise ValueError("supply-chain values must be non-empty")
        if any(
            len(values) != len(set(values))
            for values in (self.images, self.lock_paths, self.notice_paths)
        ):
            raise ValueError("supply-chain values must be unique")
        for relative in self.lock_paths + self.notice_paths:
            _validate_relative_path(relative)
        if self.sbom_mode == "committed":
            _validate_relative_path(self.sbom_ref)
        return self


class OptionalIntegration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: IntegrationName
    enabled: bool = False
    kind: IntegrationKind
    compose_profiles: tuple[str, ...] = Field(default=(), max_length=4)
    config_paths: tuple[str, ...] = Field(default=(), max_length=4)
    required_environment: tuple[str, ...] = Field(default=(), max_length=8)
    network_policy: NetworkPolicy
    output_scope: OutputScope
    affects_core_readiness: Literal[False]
    execution_authority: Literal[False]
    supply_chain: SupplyChainPolicy | None = None

    @model_validator(mode="after")
    def validate_boundary(self) -> Self:
        _require_unique(self.compose_profiles, "compose profiles")
        _require_unique(self.config_paths, "config paths")
        _require_unique(self.required_environment, "required environment")
        for relative in self.config_paths:
            _validate_relative_path(relative)
        is_future = self.name in FUTURE_RFC_INTEGRATIONS
        is_deployable = self.name in DEPLOYABLE_INTEGRATIONS
        if is_future and (
            self.enabled
            or self.kind is not IntegrationKind.FUTURE_RFC
            or self.compose_profiles
            or self.required_environment
            or self.supply_chain is not None
        ):
            raise ValueError("future RFC integrations cannot be deployable")
        if is_deployable and (not self.compose_profiles or self.supply_chain is None):
            raise ValueError(
                "deployable integrations require profiles and supply chain"
            )
        if self.name is IntegrationName.AI_TRADER and (
            self.kind is not IntegrationKind.EXTERNAL_HTTP
            or self.compose_profiles
            or self.supply_chain is not None
        ):
            raise ValueError("AI Trader must remain an external HTTP adapter")
        return self


class OptionalFeatureFlags(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ai_trader: bool = False
    openbb: bool = False
    tradingagents: bool = False
    kronos: bool = False
    qlib: bool = False
    nautilus: bool = False
    lean: bool = False
    rd_agent: bool = False
    freqtrade: bool = False
    finrl: bool = False
    vectorbt: bool = False

    def is_enabled(self, name: IntegrationName) -> bool:
        return bool(getattr(self, name.value))

    @property
    def enabled_integrations(self) -> tuple[IntegrationName, ...]:
        return tuple(name for name in IntegrationName if self.is_enabled(name))

    @property
    def any_enabled(self) -> bool:
        return bool(self.enabled_integrations)


class OptionalFeatureCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    execution_mode: Literal["paper"]
    integrations: tuple[OptionalIntegration, ...] = Field(min_length=11, max_length=11)

    @model_validator(mode="after")
    def validate_complete_stable_catalog(self) -> Self:
        if tuple(item.name for item in self.integrations) != tuple(IntegrationName):
            raise ValueError("optional integration catalog is incomplete or reordered")
        for item in self.integrations:
            boundary = INTEGRATION_BOUNDARIES[item.name]
            actual = IntegrationBoundary(
                item.kind,
                item.compose_profiles,
                item.config_paths,
                item.required_environment,
                item.network_policy,
                item.output_scope,
            )
            if actual != boundary:
                raise ValueError(f"{item.name.value} violates its fixed boundary")
        return self

    @property
    def flags(self) -> OptionalFeatureFlags:
        return OptionalFeatureFlags.model_validate(
            {item.name.value: item.enabled for item in self.integrations}
        )

    @property
    def deployable_integrations(self) -> tuple[OptionalIntegration, ...]:
        return tuple(
            item for item in self.integrations if item.name in DEPLOYABLE_INTEGRATIONS
        )

    @property
    def future_rfc_integrations(self) -> tuple[OptionalIntegration, ...]:
        return tuple(
            item for item in self.integrations if item.name in FUTURE_RFC_INTEGRATIONS
        )


class OptionalFeaturesLoadError(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("Optional feature configuration is invalid")


def load_optional_feature_flags(path: Path | None) -> OptionalFeatureFlags:
    if path is None or not path.is_file():
        return OptionalFeatureFlags()
    return load_optional_feature_catalog(path).flags


def load_optional_feature_catalog(path: Path) -> OptionalFeatureCatalog:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return OptionalFeatureCatalog.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise OptionalFeaturesLoadError(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Optional feature configuration is invalid",
                details={"file": path.name},
            )
        ) from error


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in value:
        raise ValueError("catalog paths must be relative POSIX paths")


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
