"""Closed human, service, and target-scoped authorization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)


class Role(StrEnum):
    VIEWER = "viewer"
    RESEARCHER = "researcher"
    STRATEGY_REVIEWER = "strategy_reviewer"
    PAPER_OPERATOR = "paper_operator"
    ADMIN = "admin"


class Permission(StrEnum):
    READ = "read"
    RUN_RESEARCH = "run_research"
    REVIEW_STRATEGY = "review_strategy"
    OPERATE_PAPER = "operate_paper"
    ADMINISTER = "administer"
    READ_ASSIGNED_ARTIFACT = "read_assigned_artifact"
    COMPLETE_ASSIGNED_RESEARCH = "complete_assigned_research"
    EXECUTE_ASSIGNED_PAPER = "execute_assigned_paper"
    DISPATCH_ASSIGNED_RESEARCH = "dispatch_assigned_research"
    DISPATCH_ASSIGNED_BACKTEST = "dispatch_assigned_backtest"
    DISPATCH_ASSIGNED_MARKET_DATA = "dispatch_assigned_market_data"
    PREFLIGHT_ASSIGNED_RESEARCH = "preflight_assigned_research"


class PrincipalKind(StrEnum):
    HUMAN = "human"
    SERVICE = "service"


class ServiceIdentity(StrEnum):
    CORE_RUNNER = "core_runner"
    RESEARCH_WORKER = "research_worker"
    PAPER_EXECUTOR = "paper_executor"


class ResourceKind(StrEnum):
    ACCOUNT = "account"
    STRATEGY = "strategy"
    EVALUATION = "evaluation"
    RESEARCH_RUN = "research_run"
    REPORT = "report"
    SNAPSHOT = "snapshot"
    INSTRUMENT = "instrument"
    MARKET = "market"
    PAPER_GLOBAL = "paper_global"
    ARTIFACT = "artifact"
    JOB = "job"
    BACKTEST_JOB = "backtest_job"


class AccessTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ResourceKind
    identifier: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,255}$",
    )


class LocalPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+=-]{0,254}$",
    )
    principal_kind: PrincipalKind = PrincipalKind.HUMAN
    roles: frozenset[Role] = Field(default_factory=frozenset, max_length=5)
    service_identity: ServiceIdentity | None = None
    targets: frozenset[AccessTarget] = Field(default_factory=frozenset, max_length=256)

    @model_validator(mode="after")
    def validate_principal_kind(self) -> Self:
        if self.principal_kind is PrincipalKind.HUMAN:
            if not self.roles or self.service_identity is not None:
                raise ValueError("human principals require roles only")
        elif self.roles or self.service_identity is None:
            raise ValueError("service principals require one service identity only")
        return self


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    subject: str
    permission: Permission
    target: AccessTarget | None = None


_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.RESEARCHER: frozenset({Permission.READ, Permission.RUN_RESEARCH}),
    Role.STRATEGY_REVIEWER: frozenset(
        {Permission.READ, Permission.RUN_RESEARCH, Permission.REVIEW_STRATEGY}
    ),
    Role.PAPER_OPERATOR: frozenset({Permission.READ, Permission.OPERATE_PAPER}),
    Role.ADMIN: frozenset(
        {
            Permission.READ,
            Permission.RUN_RESEARCH,
            Permission.REVIEW_STRATEGY,
            Permission.OPERATE_PAPER,
            Permission.ADMINISTER,
        }
    ),
}

_SERVICE_PERMISSIONS: dict[ServiceIdentity, frozenset[Permission]] = {
    ServiceIdentity.CORE_RUNNER: frozenset(
        {
            Permission.DISPATCH_ASSIGNED_RESEARCH,
            Permission.DISPATCH_ASSIGNED_BACKTEST,
            Permission.DISPATCH_ASSIGNED_MARKET_DATA,
            Permission.PREFLIGHT_ASSIGNED_RESEARCH,
        }
    ),
    ServiceIdentity.RESEARCH_WORKER: frozenset(
        {
            Permission.READ_ASSIGNED_ARTIFACT,
            Permission.COMPLETE_ASSIGNED_RESEARCH,
        }
    ),
    ServiceIdentity.PAPER_EXECUTOR: frozenset({Permission.EXECUTE_ASSIGNED_PAPER}),
}

_SERVICE_TARGET_KINDS: dict[Permission, frozenset[ResourceKind]] = {
    Permission.READ_ASSIGNED_ARTIFACT: frozenset({ResourceKind.ARTIFACT}),
    Permission.COMPLETE_ASSIGNED_RESEARCH: frozenset(
        {ResourceKind.JOB, ResourceKind.RESEARCH_RUN}
    ),
    Permission.EXECUTE_ASSIGNED_PAPER: frozenset({ResourceKind.ACCOUNT}),
    Permission.DISPATCH_ASSIGNED_RESEARCH: frozenset({ResourceKind.JOB}),
    Permission.DISPATCH_ASSIGNED_BACKTEST: frozenset({ResourceKind.BACKTEST_JOB}),
    Permission.DISPATCH_ASSIGNED_MARKET_DATA: frozenset({ResourceKind.MARKET}),
    Permission.PREFLIGHT_ASSIGNED_RESEARCH: frozenset({ResourceKind.JOB}),
}


def authorize(
    principal: LocalPrincipal,
    permission: Permission,
) -> Result[AuthorizationGrant]:
    if principal.principal_kind is PrincipalKind.HUMAN:
        granted = any(permission in _ROLE_PERMISSIONS[role] for role in principal.roles)
    else:
        identity = principal.service_identity
        granted = identity is not None and permission in _SERVICE_PERMISSIONS[identity]
    if not granted:
        return Failure(
            StructuredError(
                code=ErrorCode.FORBIDDEN,
                message="Permission denied",
                details={"permission": permission.value},
            )
        )
    return Success(AuthorizationGrant(subject=principal.subject, permission=permission))


def authorize_target(
    principal: LocalPrincipal,
    permission: Permission,
    target: AccessTarget,
) -> Result[AuthorizationGrant]:
    grant = authorize(principal, permission)
    if isinstance(grant, Failure):
        return grant
    if (
        principal.principal_kind is PrincipalKind.SERVICE
        and target.kind not in _SERVICE_TARGET_KINDS.get(permission, frozenset())
    ):
        return Failure(
            StructuredError(
                code=ErrorCode.FORBIDDEN,
                message="Target access denied",
                details={
                    "permission": permission.value,
                    "resource_kind": target.kind.value,
                },
            )
        )
    if Role.ADMIN in principal.roles or target in principal.targets:
        return Success(
            AuthorizationGrant(
                subject=principal.subject,
                permission=permission,
                target=target,
            )
        )
    return Failure(
        StructuredError(
            code=ErrorCode.FORBIDDEN,
            message="Target access denied",
            details={
                "permission": permission.value,
                "resource_kind": target.kind.value,
            },
        )
    )


def authorize_owned_target(
    principal: LocalPrincipal,
    permission: Permission,
    target: AccessTarget,
    owner_subject: str,
) -> Result[AuthorizationGrant]:
    """Authorize immutable ownership, an exact assignment, or explicit admin."""

    grant = authorize(principal, permission)
    if isinstance(grant, Failure):
        return grant
    if (
        principal.subject == owner_subject
        or Role.ADMIN in principal.roles
        or target in principal.targets
    ):
        return Success(
            AuthorizationGrant(
                subject=principal.subject,
                permission=permission,
                target=target,
            )
        )
    return Failure(
        StructuredError(
            code=ErrorCode.FORBIDDEN,
            message="Resource access denied",
            details={
                "permission": permission.value,
                "resource_kind": target.kind.value,
            },
        )
    )


def role_permissions(role: Role) -> frozenset[Permission]:
    return _ROLE_PERMISSIONS[role]


def service_permissions(identity: ServiceIdentity) -> frozenset[Permission]:
    return _SERVICE_PERMISSIONS[identity]
