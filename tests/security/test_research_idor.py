from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from stonks_agent.application.research.request_run import (
    read_report,
    read_run_events,
    request_research_run,
)
from stonks_agent.domain.auth import (
    AccessTarget,
    LocalPrincipal,
    ResourceKind,
    Role,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.research_run import (
    CanonicalRunEvent,
    ReportProjection,
    ResearchRunRefs,
    ResearchRunRequest,
)

NOW = datetime(2026, 7, 16, tzinfo=UTC)
SNAPSHOT_ID = UUID("61000000-0000-4000-8000-000000000001")
RUN_ID = UUID("61000000-0000-4000-8000-000000000002")
JOB_ID = UUID("61000000-0000-4000-8000-000000000003")
REPORT_HASH = "a" * 64


class ResearchStore:
    def __init__(self, snapshot_owner: str) -> None:
        self.owner = snapshot_owner
        self.owner_calls = 0
        self.submitted: list[ResearchRunRequest] = []

    def snapshot_owner(self, snapshot_id: UUID) -> Result[str]:
        assert snapshot_id == SNAPSHOT_ID
        self.owner_calls += 1
        return Success(self.owner)

    def submit(self, request: ResearchRunRequest) -> Result[ResearchRunRefs]:
        self.submitted.append(request)
        return Success(ResearchRunRefs(run_id=RUN_ID, job_id=JOB_ID))


class Events:
    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.reads = 0

    def owner_subject(self, run_id: UUID) -> Result[str]:
        assert run_id == RUN_ID
        return Success(self.owner)

    def list_after(
        self,
        run_id: UUID,
        *,
        after_sequence: int,
        limit: int,
    ) -> Result[tuple[CanonicalRunEvent, ...]]:
        del run_id, after_sequence, limit
        self.reads += 1
        return Success(())


class Reports:
    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.reads = 0

    def owner_subject(self, content_hash: str) -> Result[str]:
        assert content_hash == REPORT_HASH
        return Success(self.owner)

    def read(self, content_hash: str) -> Result[ReportProjection]:
        del content_hash
        self.reads += 1
        raise AssertionError("projection should not be read on denied path")


def request(owner: str = "user-a") -> ResearchRunRequest:
    return ResearchRunRequest(
        instrument_id="instrument-aapl",
        symbol="AAPL",
        as_of=NOW,
        snapshot_id=SNAPSHOT_ID,
        research_profile_id="balanced/1",
        model_policy_id="models/1",
        language="zh-TW",
        idempotency_key="same-client-key",
        owner_subject=owner,
        requested_at=NOW,
    )


def principal(
    subject: str,
    *targets: AccessTarget,
) -> LocalPrincipal:
    return LocalPrincipal(
        subject=subject,
        roles=frozenset({Role.RESEARCHER}),
        targets=frozenset(targets),
    )


def test_forged_request_owner_is_denied_before_snapshot_lookup() -> None:
    store = ResearchStore("user-b")

    result = request_research_run(principal("user-a"), request("user-b"), store)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN
    assert store.owner_calls == 0
    assert store.submitted == []


def test_cross_owner_snapshot_is_denied_before_submit() -> None:
    store = ResearchStore("user-b")

    result = request_research_run(principal("user-a"), request(), store)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN
    assert store.owner_calls == 1
    assert store.submitted == []


def test_exact_snapshot_assignment_allows_research_request() -> None:
    store = ResearchStore("user-b")
    target = AccessTarget(kind=ResourceKind.SNAPSHOT, identifier=str(SNAPSHOT_ID))

    result = request_research_run(principal("user-a", target), request(), store)

    assert isinstance(result, Success)
    assert store.submitted == [request()]


def test_cross_owner_run_and_report_are_denied_before_content_reads() -> None:
    events = Events("user-b")
    reports = Reports("user-b")
    caller = principal("user-a")

    event_result = read_run_events(
        caller,
        RUN_ID,
        after_sequence=0,
        limit=10,
        reader=events,
    )
    report_result = read_report(caller, REPORT_HASH, reports)

    assert isinstance(event_result, Failure)
    assert event_result.error.code is ErrorCode.FORBIDDEN
    assert isinstance(report_result, Failure)
    assert report_result.error.code is ErrorCode.FORBIDDEN
    assert events.reads == reports.reads == 0
