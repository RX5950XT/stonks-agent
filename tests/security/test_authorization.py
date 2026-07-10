from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks_agent.domain.auth import (
    LocalPrincipal,
    Permission,
    Role,
    authorize,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success


def test_viewer_has_only_read_permission() -> None:
    principal = LocalPrincipal(subject="local-user", roles={Role.VIEWER})

    assert isinstance(authorize(principal, Permission.READ), Success)
    denied = authorize(principal, Permission.RUN_RESEARCH)
    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.FORBIDDEN


@pytest.mark.parametrize(
    ("role", "permission"),
    [
        (Role.RESEARCHER, Permission.RUN_RESEARCH),
        (Role.STRATEGY_REVIEWER, Permission.REVIEW_STRATEGY),
        (Role.PAPER_OPERATOR, Permission.OPERATE_PAPER),
        (Role.ADMIN, Permission.ADMINISTER),
    ],
)
def test_each_role_gets_its_minimal_capability(
    role: Role, permission: Permission
) -> None:
    principal = LocalPrincipal(subject="local-user", roles={role})

    assert isinstance(authorize(principal, permission), Success)


def test_researcher_cannot_operate_paper_execution() -> None:
    principal = LocalPrincipal(subject="researcher", roles={Role.RESEARCHER})

    result = authorize(principal, Permission.OPERATE_PAPER)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN


def test_principal_rejects_unknown_external_fields() -> None:
    with pytest.raises(ValidationError):
        LocalPrincipal.model_validate(
            {"subject": "local-user", "roles": ["viewer"], "is_admin": True}
        )
