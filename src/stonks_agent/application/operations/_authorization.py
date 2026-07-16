"""Exact account/global authorization shared by paper operator use cases."""

from __future__ import annotations

from stonks_agent.domain.auth import (
    AccessTarget,
    LocalPrincipal,
    Permission,
    ResourceKind,
    authorize_target,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, StructuredError
from stonks_agent.domain.operations import KillSwitchScope


def authorize_paper_scope(
    principal: LocalPrincipal,
    scope: KillSwitchScope,
    account_id: str | None,
) -> Result[object]:
    target = paper_scope_target(scope, account_id)
    if isinstance(target, Failure):
        return target
    return authorize_target(principal, Permission.OPERATE_PAPER, target)


def authorize_paper_account(
    principal: LocalPrincipal,
    account_id: str,
) -> Result[object]:
    return authorize_target(
        principal,
        Permission.OPERATE_PAPER,
        AccessTarget(kind=ResourceKind.ACCOUNT, identifier=account_id),
    )


def paper_scope_target(
    scope: KillSwitchScope,
    account_id: str | None,
) -> AccessTarget | Failure:
    if scope is KillSwitchScope.GLOBAL and account_id is None:
        return AccessTarget(kind=ResourceKind.PAPER_GLOBAL, identifier="global")
    if scope is KillSwitchScope.ACCOUNT and account_id is not None:
        return AccessTarget(kind=ResourceKind.ACCOUNT, identifier=account_id)
    return Failure(
        StructuredError(
            code=ErrorCode.INVALID_INPUT,
            message="Paper operation scope is invalid",
        )
    )
