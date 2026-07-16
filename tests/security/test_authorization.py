from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks_agent.domain.auth import (
    AccessTarget,
    LocalPrincipal,
    Permission,
    PrincipalKind,
    ResourceKind,
    Role,
    ServiceIdentity,
    authorize,
    authorize_owned_target,
    authorize_target,
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


def test_human_target_access_is_exact_and_admin_bypass_is_explicit() -> None:
    account = AccessTarget(kind=ResourceKind.ACCOUNT, identifier="paper-owned")
    viewer = LocalPrincipal(
        subject="viewer-one",
        roles={Role.VIEWER},
        targets={account},
    )
    admin = LocalPrincipal(subject="admin-one", roles={Role.ADMIN})

    assert isinstance(authorize_target(viewer, Permission.READ, account), Success)
    denied = authorize_target(
        viewer,
        Permission.READ,
        AccessTarget(kind=ResourceKind.ACCOUNT, identifier="paper-other"),
    )
    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.FORBIDDEN
    assert "paper-other" not in str(denied.error.details)
    assert isinstance(
        authorize_target(
            admin,
            Permission.ADMINISTER,
            AccessTarget(kind=ResourceKind.PAPER_GLOBAL, identifier="global"),
        ),
        Success,
    )


def test_persisted_owner_or_explicit_assignment_can_access_exact_target() -> None:
    target = AccessTarget(kind=ResourceKind.STRATEGY, identifier="owned-strategy@1.0.0")
    owner = LocalPrincipal(subject="owner-one", roles={Role.VIEWER})
    assigned = LocalPrincipal(
        subject="reviewer-one",
        roles={Role.STRATEGY_REVIEWER},
        targets={target},
    )
    stranger = LocalPrincipal(subject="stranger-one", roles={Role.VIEWER})

    assert isinstance(
        authorize_owned_target(owner, Permission.READ, target, "owner-one"), Success
    )
    assert isinstance(
        authorize_owned_target(assigned, Permission.READ, target, "owner-one"), Success
    )
    denied = authorize_owned_target(
        stranger,
        Permission.READ,
        target,
        "owner-one",
    )
    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.FORBIDDEN
    assert "owner-one" not in repr(denied.error)


def test_service_identities_have_only_non_human_assigned_permissions() -> None:
    runner = LocalPrincipal(
        subject="service:core-runner",
        principal_kind=PrincipalKind.SERVICE,
        service_identity=ServiceIdentity.CORE_RUNNER,
        targets={AccessTarget(kind=ResourceKind.JOB, identifier="job-one")},
    )
    worker = LocalPrincipal(
        subject="service:research-worker",
        principal_kind=PrincipalKind.SERVICE,
        service_identity=ServiceIdentity.RESEARCH_WORKER,
        targets={
            AccessTarget(
                kind=ResourceKind.RESEARCH_RUN,
                identifier="00000000-0000-4000-8000-000000000001",
            )
        },
    )
    executor = LocalPrincipal(
        subject="service:paper-executor",
        principal_kind=PrincipalKind.SERVICE,
        service_identity=ServiceIdentity.PAPER_EXECUTOR,
        targets={AccessTarget(kind=ResourceKind.ACCOUNT, identifier="paper-main")},
    )

    assert isinstance(authorize(worker, Permission.COMPLETE_ASSIGNED_RESEARCH), Success)
    assert isinstance(authorize(executor, Permission.EXECUTE_ASSIGNED_PAPER), Success)
    assert isinstance(authorize(runner, Permission.DISPATCH_ASSIGNED_RESEARCH), Success)
    crossed = runner.model_copy(
        update={
            "targets": frozenset(
                {AccessTarget(kind=ResourceKind.MARKET, identifier="US/AAPL")}
            )
        }
    )
    assert isinstance(
        authorize_target(
            crossed,
            Permission.DISPATCH_ASSIGNED_RESEARCH,
            AccessTarget(kind=ResourceKind.MARKET, identifier="US/AAPL"),
        ),
        Failure,
    )
    for principal in (runner, worker, executor):
        assert isinstance(authorize(principal, Permission.OPERATE_PAPER), Failure)
        assert isinstance(authorize(principal, Permission.REVIEW_STRATEGY), Failure)
        assert isinstance(authorize(principal, Permission.ADMINISTER), Failure)


def test_principal_kind_role_and_target_invariants_fail_closed() -> None:
    with pytest.raises(ValidationError):
        LocalPrincipal(
            subject="service:bad",
            principal_kind=PrincipalKind.SERVICE,
            service_identity=ServiceIdentity.RESEARCH_WORKER,
            roles={Role.ADMIN},
        )
    with pytest.raises(ValidationError):
        LocalPrincipal(subject="human-bad", roles=set())
    with pytest.raises(ValidationError):
        AccessTarget(kind=ResourceKind.ACCOUNT, identifier="*")


def test_principal_rejects_unknown_external_fields() -> None:
    with pytest.raises(ValidationError):
        LocalPrincipal.model_validate(
            {"subject": "local-user", "roles": ["viewer"], "is_admin": True}
        )
