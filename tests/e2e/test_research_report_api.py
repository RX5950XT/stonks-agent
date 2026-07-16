from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.domain.auth import Role
from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.research_run import (
    CanonicalRunEvent,
    ReportProjection,
    ResearchRunRefs,
    ResearchRunRequest,
)
from stonks_agent.entrypoints.api.routes.research import create_research_app

NOW = datetime(2026, 7, 13, 8, tzinfo=UTC)
TOKEN = "research-api-token-that-is-at-least-32-chars"
RUN_ID = UUID("36000000-0000-4000-8000-000000000001")
JOB_ID = UUID("36000000-0000-4000-8000-000000000002")
EVENT_ID = UUID("36000000-0000-4000-8000-000000000003")
SNAPSHOT_ID = UUID("36000000-0000-4000-8000-000000000004")
REPORT_ID = UUID("36000000-0000-4000-8000-000000000005")
REPORT_HASH = "a" * 64


class Requests:
    def __init__(self) -> None:
        self.submitted: list[ResearchRunRequest] = []

    def submit(self, request: ResearchRunRequest) -> Result[ResearchRunRefs]:
        self.submitted.append(request)
        return Success(ResearchRunRefs(run_id=RUN_ID, job_id=JOB_ID))

    def snapshot_owner(self, snapshot_id: UUID) -> Result[str]:
        assert snapshot_id == SNAPSHOT_ID
        return Success("local-api")


class ExplodingRequests(Requests):
    def submit(self, request: ResearchRunRequest) -> Result[ResearchRunRefs]:
        del request
        raise RuntimeError("database_password=must-not-leak")


class Events:
    def __init__(self) -> None:
        self.after: list[tuple[UUID, int, int]] = []

    def list_after(
        self, run_id: UUID, *, after_sequence: int, limit: int
    ) -> Result[tuple[CanonicalRunEvent, ...]]:
        self.after.append((run_id, after_sequence, limit))
        return Success(
            (
                CanonicalRunEvent(
                    event_id=EVENT_ID,
                    run_id=run_id,
                    sequence=after_sequence + 1,
                    event_type="research.degraded",
                    payload={
                        "status": "degraded",
                        "reason": "provider_unavailable",
                        "api_token": "must-redact",
                    },
                    occurred_at=NOW,
                    event_hash="b" * 64,
                ),
            )
        )

    def owner_subject(self, run_id: UUID) -> Result[str]:
        assert run_id == RUN_ID
        return Success("local-api")


class Reports:
    def owner_subject(self, content_hash: str) -> Result[str]:
        assert content_hash == REPORT_HASH
        return Success("local-api")

    def read(self, content_hash: str) -> Result[ReportProjection]:
        assert content_hash == REPORT_HASH
        return Success(
            ReportProjection(
                report_id=REPORT_ID,
                content_hash=REPORT_HASH,
                format="markdown_full",
                media_type="text/markdown",
                content="# AAPL\nResearch only.",
            )
        )


def test_research_api_only_enqueues_and_returns_refs() -> None:
    requests = Requests()
    client = TestClient(app(requests=requests))

    response = client.post(
        "/v1/research/runs",
        headers=authorization(),
        json=research_body(),
    )

    assert response.status_code == 202
    assert response.json()["data"] == {
        "run_id": str(RUN_ID),
        "job_id": str(JOB_ID),
    }
    assert len(requests.submitted) == 1
    submitted = requests.submitted[0]
    assert submitted.requested_at == NOW
    assert submitted.owner_subject == "local-api"
    assert submitted.execution_mode == "paper"
    assert "order" not in submitted.model_dump(mode="json")


def test_research_sse_projects_only_canonical_events_and_resumes() -> None:
    events = Events()
    client = TestClient(app(events=events))

    response = client.get(
        f"/v1/research/runs/{RUN_ID}/events?limit=10",
        headers={**authorization(), "last-event-id": "4"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert events.after == [(RUN_ID, 4, 10)]
    lines = response.text.splitlines()
    assert lines[0] == "id: 5"
    assert lines[1] == "event: research.degraded"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert payload["success"] is True
    assert payload["data"]["payload"]["reason"] == "provider_unavailable"
    assert payload["data"]["payload"]["api_token"] == "[REDACTED]"
    assert "must-redact" not in response.text


def test_report_api_reads_only_typed_report_projection() -> None:
    client = TestClient(app())

    response = client.get(f"/v1/reports/{REPORT_HASH}", headers=authorization())

    assert response.status_code == 200
    assert response.json()["data"] == {
        "report_id": str(REPORT_ID),
        "content_hash": REPORT_HASH,
        "format": "markdown_full",
        "media_type": "text/markdown",
        "content": "# AAPL\nResearch only.",
    }


@pytest.mark.parametrize("content_hash", ["A" * 64, "a" * 63, "../" + "a" * 64])
def test_report_api_rejects_noncanonical_content_hash(content_hash: str) -> None:
    response = TestClient(app()).get(
        f"/v1/reports/{content_hash}",
        headers=authorization(),
    )

    assert response.status_code in {400, 404}


def test_viewer_cannot_create_research_but_can_read_events() -> None:
    client = TestClient(app(roles=frozenset({Role.VIEWER})))

    denied = client.post(
        "/v1/research/runs", headers=authorization(), json=research_body()
    )
    readable = client.get(f"/v1/research/runs/{RUN_ID}/events", headers=authorization())

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    assert readable.status_code == 200


def test_invalid_last_event_id_and_unknown_fields_use_uniform_envelope() -> None:
    client = TestClient(app())

    bad_cursor = client.get(
        f"/v1/research/runs/{RUN_ID}/events",
        headers={**authorization(), "last-event-id": "-1"},
    )
    bad_body = client.post(
        "/v1/research/runs",
        headers=authorization(),
        json={**research_body(), "order_side": "buy"},
    )

    for response in (bad_cursor, bad_body):
        assert response.status_code == 400
        assert set(response.json()) == {
            "success",
            "status",
            "data",
            "error",
            "metadata",
        }
        assert response.json()["error"]["code"] == "invalid_input"


def test_api_defaults_to_deny_and_redacts_unexpected_errors() -> None:
    denied_client = TestClient(
        create_research_app(Requests(), Events(), Reports(), clock=lambda: NOW)
    )
    denied = denied_client.post(
        "/v1/research/runs", headers=authorization(), json=research_body()
    )
    exploding_client = TestClient(
        app(requests=ExplodingRequests()), raise_server_exceptions=False
    )
    failed = exploding_client.post(
        "/v1/research/runs", headers=authorization(), json=research_body()
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "unauthorized"
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "internal_error"
    assert "database_password" not in failed.text


def app(
    *,
    requests: Requests | None = None,
    events: Events | None = None,
    roles: frozenset[Role] = frozenset({Role.RESEARCHER}),
) -> object:
    return create_research_app(
        requests or Requests(),
        events or Events(),
        Reports(),
        LocalTokenAuthenticator(
            environment="test",
            token=TOKEN,
            subject="local-api",
            roles=roles,
            allowed_hosts=frozenset({"testclient"}),
        ),
        clock=lambda: NOW,
    )


def authorization() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}"}


def research_body() -> dict[str, object]:
    return {
        "instrument_id": "instrument-aapl",
        "symbol": "AAPL",
        "as_of": NOW.isoformat(),
        "snapshot_id": str(SNAPSHOT_ID),
        "research_profile_id": "balanced/1",
        "model_policy_id": "research-models/1",
        "language": "zh-TW",
        "idempotency_key": "research-api-1",
    }
