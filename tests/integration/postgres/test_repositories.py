from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.workflow import CreateWorkflowRun, WorkflowStatus
from stonks_contracts.evidence import EvidenceItem, EvidenceKind, Sensitivity
from stonks_contracts.market_data import DataQuality, DataQualityStatus

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
ARTIFACT_HASH = "e" * 64


def test_unit_of_work_rolls_back_without_explicit_commit(
    clean_database: Engine,
) -> None:
    with clean_database.begin() as connection:
        _insert_artifact(connection)

    with PostgresUnitOfWork(clean_database) as uow:
        result = uow.evidence.append(evidence())
        assert isinstance(result, Success)

    with clean_database.connect() as connection:
        count = connection.scalar(text("select count(*) from evidence_item"))
    assert count == 0


def test_evidence_round_trip_and_point_in_time_query(clean_database: Engine) -> None:
    with clean_database.begin() as connection:
        _insert_artifact(connection)
    expected = evidence()

    with PostgresUnitOfWork(clean_database) as uow:
        assert isinstance(uow.evidence.append(expected), Success)
        uow.commit()

    with PostgresUnitOfWork(clean_database) as uow:
        loaded = uow.evidence.get(expected.evidence_id)
        visible = uow.evidence.query_available(
            subject="AAPL",
            as_of=NOW,
        )

    assert isinstance(loaded, Success)
    assert loaded.value == expected
    assert isinstance(visible, Success)
    assert visible.value == (expected,)


def test_duplicate_evidence_is_structured_conflict(clean_database: Engine) -> None:
    with clean_database.begin() as connection:
        _insert_artifact(connection)
    item = evidence()

    with PostgresUnitOfWork(clean_database) as uow:
        assert isinstance(uow.evidence.append(item), Success)
        uow.commit()
    with PostgresUnitOfWork(clean_database) as uow:
        duplicate = uow.evidence.append(item)

    assert isinstance(duplicate, Failure)
    assert duplicate.error.code is ErrorCode.CONFLICT


def test_run_idempotency_rejects_different_payload(clean_database: Engine) -> None:
    request = create_run(input_hash="a" * 64)
    with PostgresUnitOfWork(clean_database) as uow:
        first = uow.workflows.create(request)
        uow.commit()

    with PostgresUnitOfWork(clean_database) as uow:
        same = uow.workflows.create(request)
        conflict = uow.workflows.create(create_run(input_hash="b" * 64))

    assert isinstance(first, Success)
    assert isinstance(same, Success)
    assert same.value == first.value
    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT


def test_concurrent_run_transition_uses_compare_and_swap(
    clean_database: Engine,
) -> None:
    request = create_run(input_hash="a" * 64)
    with PostgresUnitOfWork(clean_database) as uow:
        created = uow.workflows.create(request)
        assert isinstance(created, Success)
        uow.commit()

    def transition(status: WorkflowStatus) -> object:
        with PostgresUnitOfWork(clean_database) as uow:
            result = uow.workflows.transition(
                request.run_id,
                expected_version=1,
                new_status=status,
                updated_at=NOW + timedelta(seconds=1),
            )
            if isinstance(result, Success):
                uow.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                transition,
                (WorkflowStatus.RUNNING, WorkflowStatus.CANCELLED),
            )
        )

    assert sum(isinstance(result, Success) for result in results) == 1
    failed = next(result for result in results if isinstance(result, Failure))
    assert failed.error.code is ErrorCode.CONFLICT


def evidence() -> EvidenceItem:
    return EvidenceItem(
        evidence_id=UUID("20000000-0000-4000-8000-000000000001"),
        subject="AAPL",
        kind=EvidenceKind.MARKET_DATA,
        payload={"close": "100.00"},
        event_time=NOW - timedelta(minutes=2),
        published_at=NOW - timedelta(minutes=1),
        available_at=NOW,
        observed_at=NOW,
        as_of=NOW,
        source="fixture",
        provider="replay",
        content_hash="f" * 64,
        raw_artifact_ref=f"sha256:{ARTIFACT_HASH}",
        quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal("1"),
        ),
        sensitivity=Sensitivity.INTERNAL,
        license_tag="test-only",
        redistribution_tag="none",
    )


def create_run(*, input_hash: str) -> CreateWorkflowRun:
    return CreateWorkflowRun(
        run_id=UUID("30000000-0000-4000-8000-000000000001"),
        run_type="ingestion",
        as_of=NOW,
        policy_id="policy/1",
        idempotency_key="run-idempotency",
        input_hash=input_hash,
        created_at=NOW,
    )


def _insert_artifact(connection: Connection) -> None:
    connection.execute(
        text(
            """
            insert into artifact_manifest
                (content_hash, size_bytes, media_type, license_tag, sensitivity,
                 source, finalized_at, storage_uri, metadata)
            values
                (:hash, 1, 'application/json', 'test-only', 'internal',
                 'fixture', :now, :uri, '{}'::jsonb)
            """
        ),
        {
            "hash": ARTIFACT_HASH,
            "now": NOW,
            "uri": f"artifact://sha256/{ARTIFACT_HASH}",
        },
    )
