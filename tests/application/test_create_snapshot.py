from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from stonks_agent.application.data.create_snapshot import request_snapshot
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.snapshot import CreateSnapshotRequest, SnapshotJobRefs

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
REFS = SnapshotJobRefs(
    run_id=UUID("70000000-0000-4000-8000-000000000001"),
    job_id=UUID("70000000-0000-4000-8000-000000000002"),
    snapshot_id=UUID("70000000-0000-4000-8000-000000000003"),
    evidence_refs=(),
)


class FakeSnapshotStore:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, request: CreateSnapshotRequest) -> Result[SnapshotJobRefs]:
        del request
        self.calls += 1
        return Success(REFS)


def snapshot_request() -> CreateSnapshotRequest:
    return CreateSnapshotRequest(
        market="US",
        capability="prices",
        as_of=NOW,
        query={"symbol": "AAPL"},
        provider_policy_id="us-prices/1",
        idempotency_key="snapshot-request-1",
        requested_at=NOW,
    )


def test_researcher_can_request_snapshot_job() -> None:
    store = FakeSnapshotStore()
    principal = LocalPrincipal(
        subject="researcher",
        roles=frozenset({Role.RESEARCHER}),
    )

    result = request_snapshot(principal, snapshot_request(), store)

    assert isinstance(result, Success)
    assert result.value == REFS
    assert store.calls == 1


def test_viewer_cannot_enqueue_snapshot_job() -> None:
    store = FakeSnapshotStore()
    principal = LocalPrincipal(
        subject="viewer",
        roles=frozenset({Role.VIEWER}),
    )

    result = request_snapshot(principal, snapshot_request(), store)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN
    assert store.calls == 0
