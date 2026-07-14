from __future__ import annotations

from datetime import timedelta

from application.operations.test_use_cases import ACTION_ID, NOW, Factory
from fastapi.testclient import TestClient

from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.domain.auth import Role
from stonks_agent.entrypoints.api.routes.operations import (
    create_paper_operations_app,
)

TOKEN = "paper-operator-test-token-that-is-long-enough"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}


def app(
    factory: Factory,
    *,
    roles: frozenset[Role] = frozenset({Role.PAPER_OPERATOR}),
):  # type: ignore[no-untyped-def]
    return create_paper_operations_app(
        factory,
        LocalTokenAuthenticator(
            token=TOKEN,
            subject="operator:one",
            roles=roles,
            allowed_hosts=frozenset({"testclient"}),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )


def activation_body() -> dict[str, object]:
    return {
        "action_id": str(ACTION_ID),
        "scope": "global",
        "account_id": None,
        "expected_version": 1,
        "reason_code": "operator_requested",
    }


def test_operator_api_uses_authenticated_actor_and_uniform_envelope() -> None:
    factory = Factory()
    client = TestClient(app(factory))

    response = client.post(
        "/v1/paper/kill-switches/activate",
        json=activation_body(),
        headers=AUTHORIZATION,
    )
    actions = client.get(
        "/v1/paper/operator-actions",
        headers=AUTHORIZATION,
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"]["action"]["actor"] == "operator:one"
    assert actions.status_code == 200
    assert actions.json()["data"][0]["action_type"] == "activated"


def test_operator_api_rejects_non_operator_and_extra_actor_field() -> None:
    factory = Factory()
    researcher = TestClient(app(factory, roles=frozenset({Role.RESEARCHER})))
    operator = TestClient(app(factory))

    forbidden = researcher.post(
        "/v1/paper/kill-switches/activate",
        json=activation_body(),
        headers=AUTHORIZATION,
    )
    forged = operator.post(
        "/v1/paper/kill-switches/activate",
        json=activation_body() | {"actor": "admin:forged"},
        headers=AUTHORIZATION,
    )

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"
    assert forged.status_code == 400
    assert factory.uow.commits == 0


def test_reconcile_and_resume_routes_call_same_authorized_use_cases() -> None:
    factory = Factory()
    client = TestClient(app(factory))

    reconciled = client.post(
        "/v1/paper/reconciliation",
        json={"action_id": str(ACTION_ID), "account_id": "paper-ledger"},
        headers=AUTHORIZATION,
    )
    resumed = client.post(
        "/v1/paper/kill-switches/resume",
        json={
            "action_id": str(ACTION_ID),
            "scope": "global",
            "account_id": None,
            "expected_version": 2,
            "reason_code": "reconciliation_passed",
        },
        headers=AUTHORIZATION,
    )
    status = client.get(
        "/v1/paper/kill-switches/global",
        headers=AUTHORIZATION,
    )

    assert reconciled.status_code == resumed.status_code == status.status_code == 200
    assert factory.uow.commits == 2
