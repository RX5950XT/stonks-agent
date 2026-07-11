"""Validated provider allowlists, fallback order, and reconciliation policy."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Protocol, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks_agent.domain.data_quality import ProviderDataState, ProviderObservation
from stonks_contracts.common import DecimalString, NonEmptyString, UnitDecimal


class ProviderRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    origin: str = Field(min_length=1, max_length=512)
    endpoints: tuple[str, ...] = Field(min_length=1)
    freshness_seconds: int = Field(ge=0)
    quota_floor: int = Field(ge=0)

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        if value.strip() != value or any(ord(character) < 32 for character in value):
            raise ValueError("provider origin must not contain whitespace or controls")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as error:
            raise ValueError(
                "provider origin must contain a valid host and port"
            ) from error
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("provider origin must contain a valid host and port")
        secure_origin = parsed.scheme == "https"
        loopback_origin = parsed.scheme == "http" and hostname in {
            "127.0.0.1",
            "::1",
        }
        host_literal = f"[{hostname}]" if ":" in hostname else hostname
        canonical_netloc = host_literal if port is None else f"{host_literal}:{port}"
        if (
            not (secure_origin or loopback_origin)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc.lower() != canonical_netloc.lower()
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "provider origin must be credential-free HTTPS or exact loopback HTTP"
            )
        return value.rstrip("/")

    @field_validator("endpoints")
    @classmethod
    def validate_endpoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("provider endpoints must be unique")
        if any(
            not endpoint.startswith("/")
            or endpoint.startswith("//")
            or "://" in endpoint
            for endpoint in value
        ):
            raise ValueError("provider endpoints must be relative allowlisted paths")
        return value


class ProviderPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: NonEmptyString
    market: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    capability: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    routes: tuple[ProviderRoute, ...] = Field(min_length=1)
    allow_stale: bool = False
    allow_partial: bool = False
    reconciliation_threshold: UnitDecimal

    @model_validator(mode="after")
    def validate_routes(self) -> Self:
        providers = tuple(route.provider for route in self.routes)
        if len(providers) != len(set(providers)):
            raise ValueError("provider fallback routes must be unique")
        return self


class ReconciliationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ProviderDataState
    relative_difference: UnitDecimal
    reasons: tuple[str, ...] = ()


class ReconciliationValue(BaseModel):
    """A provider-independent scalar with an explicit comparison dimension."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str = Field(min_length=1, max_length=128)
    value: DecimalString

    @field_validator("metric")
    @classmethod
    def validate_metric(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("reconciliation metric must not contain edge whitespace")
        return value


class ReconciliationStrategy[T](Protocol):
    """Extract a comparable scalar without exposing raw provider payloads."""

    def extract(
        self,
        provider: str,
        observation: ProviderObservation[T],
    ) -> ReconciliationValue | None: ...


def reconcile_values(
    primary: Decimal,
    secondary: Decimal,
    policy: ProviderPolicy,
) -> ReconciliationDecision:
    denominator = max(abs(primary), abs(secondary), Decimal("0.00000001"))
    difference = min(abs(primary - secondary) / denominator, Decimal("1"))
    if difference > policy.reconciliation_threshold:
        return ReconciliationDecision(
            state=ProviderDataState.CONFLICT,
            relative_difference=difference,
            reasons=("reconciliation_threshold_exceeded",),
        )
    return ReconciliationDecision(
        state=ProviderDataState.AVAILABLE,
        relative_difference=difference,
    )


def reconcile_comparable_values(
    primary: ReconciliationValue,
    secondary: ReconciliationValue,
    policy: ProviderPolicy,
) -> ReconciliationDecision:
    if primary.metric != secondary.metric:
        return ReconciliationDecision(
            state=ProviderDataState.CONFLICT,
            relative_difference=Decimal("1"),
            reasons=("reconciliation_metric_mismatch",),
        )
    return reconcile_values(primary.value, secondary.value, policy)


def load_provider_policies(path: Path) -> tuple[ProviderPolicy, ...]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError("provider policy file could not be loaded") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("policies"), list):
        raise ValueError("provider policy file must contain a policies list")
    policies = tuple(ProviderPolicy.model_validate(item) for item in raw["policies"])
    keys = tuple((item.market, item.capability) for item in policies)
    if len(keys) != len(set(keys)):
        raise ValueError("provider policy market/capability keys must be unique")
    return policies
