from __future__ import annotations

from pathlib import Path

import pytest

from stonks_agent.config.rbac import RBACPolicyLoadError, load_rbac_policy
from stonks_agent.domain.auth import Permission, Role, ServiceIdentity

ROOT = Path(__file__).resolve().parents[2]


def test_committed_rbac_policy_matches_closed_domain_permissions() -> None:
    policy = load_rbac_policy(ROOT / "config" / "rbac.yaml")

    assert tuple(item.role for item in policy.roles) == tuple(Role)
    assert tuple(item.identity for item in policy.service_identities) == tuple(
        ServiceIdentity
    )
    assert policy.claims.roles == "stonks_roles"
    assert policy.claims.targets == "stonks_targets"
    assert policy.claims.service_identity == "stonks_service_identity"
    assert policy.admin_all_targets is True
    assert tuple(item.claim_values for item in policy.roles) == (
        ("stonks:viewer",),
        ("stonks:researcher",),
        ("stonks:strategy-reviewer",),
        ("stonks:paper-operator",),
        ("stonks:admin",),
    )
    assert tuple(item.subjects for item in policy.service_identities) == (
        ("service:core-runner",),
        ("service:research-worker",),
        ("service:paper-executor",),
    )
    assert tuple(item.client_ids for item in policy.service_identities) == (
        ("stonks-core-runner",),
        ("stonks-research-worker",),
        ("stonks-paper-executor",),
    )
    human_permissions = {
        permission for item in policy.roles for permission in item.permissions
    }
    assert Permission.COMPLETE_ASSIGNED_RESEARCH not in human_permissions
    assert Permission.EXECUTE_ASSIGNED_PAPER not in human_permissions
    assert all(
        Permission.OPERATE_PAPER not in item.permissions
        and Permission.REVIEW_STRATEGY not in item.permissions
        and Permission.ADMINISTER not in item.permissions
        for item in policy.service_identities
    )


@pytest.mark.parametrize(
    "payload",
    [
        "schema_version: 1\nroles: []\n",
        "schema_version: 1\nroles: [{role: superuser, permissions: [administer]}]\n",
        "schema_version: 1\nroles: [{role: viewer, permissions: [administer]}]\n",
        (
            "schema_version: 1\nroles:\n"
            "  - {role: viewer, permissions: [read], claim_values: [admin]}\n"
        ),
        "schema_version: 2\n",
    ],
)
def test_incomplete_unknown_or_privilege_drift_policy_fails_closed(
    tmp_path: Path,
    payload: str,
) -> None:
    path = tmp_path / "rbac.yaml"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(RBACPolicyLoadError) as raised:
        load_rbac_policy(path)

    assert raised.value.error.code.value == "configuration_invalid"
    assert raised.value.error.details == {"file": "rbac.yaml"}
