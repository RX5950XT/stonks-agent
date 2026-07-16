"""Closed service identities and exact dispatch authorization."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ServiceIdentity(StrEnum):
    CORE_RUNNER = "core_runner"


class ServicePermission(StrEnum):
    DISPATCH_ASSIGNED_RESEARCH = "dispatch_assigned_research"
    DISPATCH_ASSIGNED_BACKTEST = "dispatch_assigned_backtest"
    DISPATCH_ASSIGNED_MARKET_DATA = "dispatch_assigned_market_data"
    PREFLIGHT_ASSIGNED_RESEARCH = "preflight_assigned_research"


class ServiceResourceKind(StrEnum):
    JOB = "job"
    BACKTEST_JOB = "backtest_job"
    MARKET = "market"


class ServiceReceiver(StrEnum):
    KRONOS = "kronos"
    TRADINGAGENTS = "tradingagents"
    QUANT_LAB = "quant_lab"
    NAUTILUS = "nautilus"
    LEAN = "lean"
    OPENBB = "openbb"


class ServiceAccessTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ServiceResourceKind
    identifier: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,255}$",
    )


class ServicePrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+=-]{0,254}$",
    )
    identity: ServiceIdentity
    receiver: ServiceReceiver
    permission: ServicePermission
    targets: frozenset[ServiceAccessTarget] = Field(min_length=1, max_length=256)
    attempt_generation: int = Field(ge=0)
    attempt_nonce_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    token_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+=-]{0,254}$",
    )
    issued_at: int = Field(gt=0)
    expires_at: int = Field(gt=0)


class ServiceAuthenticator(Protocol):
    def authenticate(self, authorization: str | None) -> ServicePrincipal | None: ...


_PERMISSIONS: dict[
    ServiceIdentity,
    dict[ServicePermission, frozenset[ServiceResourceKind]],
] = {
    ServiceIdentity.CORE_RUNNER: {
        ServicePermission.DISPATCH_ASSIGNED_RESEARCH: frozenset(
            {ServiceResourceKind.JOB}
        ),
        ServicePermission.DISPATCH_ASSIGNED_BACKTEST: frozenset(
            {ServiceResourceKind.BACKTEST_JOB}
        ),
        ServicePermission.DISPATCH_ASSIGNED_MARKET_DATA: frozenset(
            {ServiceResourceKind.MARKET}
        ),
        ServicePermission.PREFLIGHT_ASSIGNED_RESEARCH: frozenset(
            {ServiceResourceKind.JOB}
        ),
    }
}


def authorize_service_target(
    principal: ServicePrincipal,
    permission: ServicePermission,
    target: ServiceAccessTarget,
) -> bool:
    """Grant only a server-defined permission/kind pair and exact assignment."""

    allowed_kinds = _PERMISSIONS[principal.identity].get(permission, frozenset())
    return (
        principal.permission is permission
        and target.kind in allowed_kinds
        and target in principal.targets
    )


def authorize_service_dispatch(
    principal: ServicePrincipal,
    *,
    permission: ServicePermission,
    target: ServiceAccessTarget,
    receiver: ServiceReceiver,
    attempt_generation: int,
    attempt_nonce: str,
    request_payload: dict[str, object],
    deadline: datetime | None,
) -> bool:
    """Bind authority to one receiver, payload, attempt fence, and deadline."""

    deadline_valid = deadline is None or principal.expires_at <= int(
        deadline.timestamp()
    )
    nonce_valid = (
        principal.attempt_nonce_hash == principal.request_hash
        if attempt_generation == 0
        else principal.attempt_nonce_hash == service_nonce_hash(attempt_nonce)
    )
    return (
        authorize_service_target(principal, permission, target)
        and principal.receiver is receiver
        and principal.attempt_generation == attempt_generation
        and nonce_valid
        and principal.request_hash == canonical_request_hash(request_payload)
        and deadline_valid
    )


def service_nonce_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_request_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
