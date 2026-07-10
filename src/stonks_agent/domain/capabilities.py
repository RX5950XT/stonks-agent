"""Deny-by-default process capabilities and HTTPS egress allowlists."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
from urllib.parse import SplitResult, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)


class Capability(StrEnum):
    NETWORK_EGRESS = "network_egress"
    FILESYSTEM_WRITE = "filesystem_write"
    PROCESS_SPAWN = "process_spawn"
    SECRET_READ = "secret_read"
    QUEUE_MUTATION = "queue_mutation"
    EXECUTION = "execution"


class EgressPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_origins: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: frozenset[str]) -> frozenset[str]:
        normalized: set[str] = set()
        for value in values:
            parsed = _safe_split(value)
            origin = _origin(parsed) if parsed is not None else None
            if (
                origin is None
                or parsed is None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("allowed origins must be safe HTTPS origins")
            normalized.add(origin)
        return frozenset(normalized)


class ProcessCapabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    allowed: frozenset[Capability] = Field(default_factory=frozenset)
    egress: EgressPolicy = Field(default_factory=EgressPolicy)


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    profile: str
    capability: Capability


@dataclass(frozen=True, slots=True)
class EgressGrant:
    profile: str
    origin: str
    url: str


def authorize_capability(
    policy: ProcessCapabilityPolicy,
    capability: Capability,
) -> Result[CapabilityGrant]:
    if capability not in policy.allowed:
        return Failure(
            StructuredError(
                code=ErrorCode.CAPABILITY_DENIED,
                message="Process capability denied",
                details={"capability": capability.value, "profile": policy.profile},
            )
        )
    return Success(CapabilityGrant(profile=policy.profile, capability=capability))


def authorize_egress(
    policy: ProcessCapabilityPolicy,
    url: object,
) -> Result[EgressGrant]:
    if Capability.NETWORK_EGRESS not in policy.allowed or not isinstance(url, str):
        return _egress_denied(policy.profile)
    parsed = _safe_split(url)
    origin = _origin(parsed) if parsed is not None else None
    if origin is None or origin not in policy.egress.allowed_origins:
        return _egress_denied(policy.profile)
    return Success(EgressGrant(profile=policy.profile, origin=origin, url=url))


def _safe_split(value: str) -> SplitResult | None:
    try:
        return urlsplit(value)
    except (TypeError, ValueError):
        return None


def _origin(parsed: SplitResult | None) -> str | None:
    if parsed is None or parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return None
    host = parsed.hostname.lower().rstrip(".")
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return f"https://{host}" if port in {None, 443} else f"https://{host}:{port}"


def _egress_denied(profile: str) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.EGRESS_DENIED,
            message="Network egress denied",
            details={"profile": profile},
        )
    )
