from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.domain.auth import Role
from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.snapshot import CreateSnapshotRequest, SnapshotJobRefs
from stonks_agent.entrypoints.api.routes.data import create_data_app

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
TOKEN = "test-local-token-that-is-at-least-32-chars"


class FakeSnapshotStore:
    def submit(self, request: CreateSnapshotRequest) -> Result[SnapshotJobRefs]:
        del request
        return Success(
            SnapshotJobRefs(
                run_id=UUID("70000000-0000-4000-8000-000000000001"),
                job_id=UUID("70000000-0000-4000-8000-000000000002"),
                snapshot_id=UUID("70000000-0000-4000-8000-000000000003"),
                evidence_refs=(),
            )
        )


def test_data_api_returns_only_job_snapshot_and_evidence_refs() -> None:
    client = TestClient(create_data_app(FakeSnapshotStore(), authenticator()))

    response = client.post(
        "/v1/data/snapshots",
        headers={
            "authorization": f"Bearer {TOKEN}",
        },
        json={
            "market": "US",
            "capability": "prices",
            "as_of": NOW.isoformat(),
            "query": {"symbol": "AAPL"},
            "provider_policy_id": "us-prices/1",
            "idempotency_key": "api-request-1",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["success"] is True
    assert set(payload["data"]) == {
        "run_id",
        "job_id",
        "snapshot_id",
        "evidence_refs",
    }
    assert "payload" not in payload["data"]


def test_data_api_rejects_missing_local_identity() -> None:
    client = TestClient(create_data_app(FakeSnapshotStore(), authenticator()))

    response = client.post(
        "/v1/data/snapshots",
        json={
            "market": "US",
            "capability": "prices",
            "as_of": NOW.isoformat(),
            "query": {"symbol": "AAPL"},
            "provider_policy_id": "us-prices/1",
            "idempotency_key": "api-request-1",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_data_api_does_not_trust_user_supplied_role_headers() -> None:
    client = TestClient(create_data_app(FakeSnapshotStore(), authenticator()))

    response = client.post(
        "/v1/data/snapshots",
        headers={
            "x-local-subject": "attacker",
            "x-local-roles": "admin,researcher",
        },
        json={
            "market": "US",
            "capability": "prices",
            "as_of": NOW.isoformat(),
            "query": {"symbol": "AAPL"},
            "provider_policy_id": "us-prices/1",
            "idempotency_key": "api-request-forged-role",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_data_api_fails_closed_without_configured_authenticator() -> None:
    client = TestClient(create_data_app(FakeSnapshotStore()))

    response = client.post(
        "/v1/data/snapshots",
        headers={"authorization": f"Bearer {TOKEN}"},
        json={
            "market": "US",
            "capability": "prices",
            "as_of": NOW.isoformat(),
            "query": {"symbol": "AAPL"},
            "provider_policy_id": "us-prices/1",
            "idempotency_key": "api-request-no-authenticator",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def authenticator() -> LocalTokenAuthenticator:
    return LocalTokenAuthenticator(
        token=TOKEN,
        subject="local-researcher",
        roles=frozenset({Role.RESEARCHER}),
        allowed_hosts=frozenset({"testclient"}),
    )
