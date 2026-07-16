from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from httpx import Response

from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.domain.auth import Role
from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.snapshot import CreateSnapshotRequest, SnapshotJobRefs
from stonks_agent.entrypoints.api.routes.data import (
    MAX_SNAPSHOT_REQUEST_BYTES,
    create_data_app,
)

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


class ExplodingSnapshotStore:
    def submit(self, request: CreateSnapshotRequest) -> Result[SnapshotJobRefs]:
        del request
        raise RuntimeError("database_password=must-not-leak")


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


def test_data_api_wraps_framework_validation_in_uniform_envelope() -> None:
    client = TestClient(create_data_app(FakeSnapshotStore(), authenticator()))

    response = client.post(
        "/v1/data/snapshots",
        headers={"authorization": f"Bearer {TOKEN}"},
        json={
            "market": "us",
            "capability": "prices",
            "as_of": NOW.isoformat(),
            "query": {"symbol": "AAPL"},
            "provider_policy_id": "us-prices/1",
            "idempotency_key": "api-invalid-market",
            "unexpected": "must be rejected",
        },
    )

    _assert_error_envelope(response, status=400, code="invalid_input")


def test_data_api_wraps_malformed_json_in_uniform_envelope() -> None:
    client = TestClient(create_data_app(FakeSnapshotStore(), authenticator()))

    response = client.post(
        "/v1/data/snapshots",
        headers={
            "authorization": f"Bearer {TOKEN}",
            "content-type": "application/json",
        },
        content=b'{"market":',
    )

    _assert_error_envelope(response, status=400, code="invalid_input")


def test_data_api_rejects_oversized_body_before_validation() -> None:
    client = TestClient(create_data_app(FakeSnapshotStore(), authenticator()))
    body = json.dumps(
        {
            "market": "US",
            "capability": "prices",
            "as_of": NOW.isoformat(),
            "query": {"blob": "x" * MAX_SNAPSHOT_REQUEST_BYTES},
            "provider_policy_id": "us-prices/1",
            "idempotency_key": "api-oversized",
        }
    ).encode()

    response = client.post(
        "/v1/data/snapshots",
        headers={
            "authorization": f"Bearer {TOKEN}",
            "content-type": "application/json",
        },
        content=body,
    )

    _assert_error_envelope(response, status=413, code="payload_too_large")


def test_data_api_wraps_oversized_authorization_validation_error() -> None:
    secret = "x" * 4097
    client = TestClient(create_data_app(FakeSnapshotStore(), authenticator()))

    response = client.post(
        "/v1/data/snapshots",
        headers={"authorization": f"Bearer {secret}"},
        json={
            "market": "US",
            "capability": "prices",
            "as_of": NOW.isoformat(),
            "query": {"symbol": "AAPL"},
            "provider_policy_id": "us-prices/1",
            "idempotency_key": "api-oversized-authorization",
        },
    )

    _assert_error_envelope(response, status=401, code="unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"
    assert secret not in response.text


def test_data_api_redacts_unexpected_exception_in_uniform_500_envelope() -> None:
    client = TestClient(
        create_data_app(ExplodingSnapshotStore(), authenticator()),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/v1/data/snapshots",
        headers={"authorization": f"Bearer {TOKEN}"},
        json={
            "market": "US",
            "capability": "prices",
            "as_of": NOW.isoformat(),
            "query": {"symbol": "AAPL"},
            "provider_policy_id": "us-prices/1",
            "idempotency_key": "api-unexpected-error",
        },
    )

    _assert_error_envelope(response, status=500, code="internal_error")
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": "Internal server error",
        "details": {},
    }
    assert "database_password" not in response.text


def authenticator() -> LocalTokenAuthenticator:
    return LocalTokenAuthenticator(
        environment="test",
        token=TOKEN,
        subject="local-researcher",
        roles=frozenset({Role.RESEARCHER}),
        allowed_hosts=frozenset({"testclient"}),
    )


def _assert_error_envelope(
    response: Response,
    *,
    status: int,
    code: str,
) -> None:
    assert response.status_code == status
    payload = response.json()
    assert set(payload) == {"success", "status", "data", "error", "metadata"}
    assert payload["success"] is False
    assert payload["status"] == status
    assert payload["data"] is None
    assert payload["error"]["code"] == code
