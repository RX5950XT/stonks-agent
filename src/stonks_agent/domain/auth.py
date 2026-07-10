"""Local identity and minimal role-based authorization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

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


class LocalPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:@-]+$")
    roles: frozenset[Role] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    subject: str
    permission: Permission


_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.RESEARCHER: frozenset({Permission.READ, Permission.RUN_RESEARCH}),
    Role.STRATEGY_REVIEWER: frozenset(
        {Permission.READ, Permission.RUN_RESEARCH, Permission.REVIEW_STRATEGY}
    ),
    Role.PAPER_OPERATOR: frozenset({Permission.READ, Permission.OPERATE_PAPER}),
    Role.ADMIN: frozenset(Permission),
}


def authorize(
    principal: LocalPrincipal,
    permission: Permission,
) -> Result[AuthorizationGrant]:
    granted = any(permission in _ROLE_PERMISSIONS[role] for role in principal.roles)
    if not granted:
        return Failure(
            StructuredError(
                code=ErrorCode.FORBIDDEN,
                message="Permission denied",
                details={"permission": permission.value},
            )
        )
    return Success(AuthorizationGrant(subject=principal.subject, permission=permission))
