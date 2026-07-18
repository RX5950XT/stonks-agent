from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from stonks_agent.adapters.postgres.artifact_maintenance_audit import (
    PostgresArtifactMaintenanceAudit,
)
from stonks_agent.domain.artifact_retention import (
    ArtifactMaintenanceAction,
    ArtifactMaintenanceAuditEvent,
    ArtifactMaintenancePhase,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success

pytestmark = pytest.mark.postgres

CONTENT_HASH = "a" * 64
CALLER_TIME = datetime(2020, 1, 2, 3, tzinfo=UTC)
OPERATION_ID = UUID("a1000000-0000-4000-8000-000000000001")
REQUEST_ID = UUID("a2000000-0000-4000-8000-000000000001")
TERMINAL_ID = UUID("a3000000-0000-4000-8000-000000000001")
COMMAND_HASH = "c" * 64
RESULT_HASH = "d" * 64


def audit_event(
    *,
    event_id: UUID = REQUEST_ID,
    operation_id: UUID = OPERATION_ID,
    action: ArtifactMaintenanceAction = ArtifactMaintenanceAction.RESTORE,
    phase: ArtifactMaintenancePhase = ArtifactMaintenancePhase.REQUESTED,
    content_hash: str | None = CONTENT_HASH,
    actor: str = "operator:postgres",
    reason: str = "operator_requested",
    command_hash: str = COMMAND_HASH,
    result_hash: str | None = None,
    occurred_at: datetime = CALLER_TIME,
    outcome: str | None = None,
    previous_event_hash: str | None = None,
) -> ArtifactMaintenanceAuditEvent:
    if phase is not ArtifactMaintenancePhase.REQUESTED and result_hash is None:
        result_hash = RESULT_HASH
    return ArtifactMaintenanceAuditEvent.create(
        event_id=event_id,
        operation_id=operation_id,
        action=action,
        phase=phase,
        content_hash=content_hash,
        actor=actor,
        reason=reason,
        command_hash=command_hash,
        result_hash=result_hash,
        occurred_at=occurred_at,
        outcome=outcome,
        previous_event_hash=previous_event_hash,
    )


def record_request(
    engine: Engine,
    *,
    event_id: UUID = REQUEST_ID,
    operation_id: UUID = OPERATION_ID,
) -> ArtifactMaintenanceAuditEvent:
    result = PostgresArtifactMaintenanceAudit(engine).record(
        audit_event(event_id=event_id, operation_id=operation_id)
    )
    assert isinstance(result, Success)
    return result.value


def terminal_for(
    requested: ArtifactMaintenanceAuditEvent,
    *,
    event_id: UUID = TERMINAL_ID,
    actor: str | None = None,
    reason: str | None = None,
) -> ArtifactMaintenanceAuditEvent:
    return audit_event(
        event_id=event_id,
        operation_id=requested.operation_id,
        action=requested.action,
        phase=ArtifactMaintenancePhase.COMPLETED,
        content_hash=requested.content_hash,
        actor=actor or requested.actor,
        reason=reason or requested.reason,
        occurred_at=CALLER_TIME + timedelta(seconds=1),
        outcome="restored",
        previous_event_hash=requested.event_hash,
    )


def test_record_uses_database_time_and_rebuilds_canonical_hash(
    clean_database: Engine,
) -> None:
    with clean_database.connect() as connection:
        before = connection.scalar(text("select clock_timestamp()"))
    caller = audit_event()

    result = PostgresArtifactMaintenanceAudit(clean_database).record(caller)

    assert isinstance(result, Success)
    recorded = result.value
    with clean_database.connect() as connection:
        after = connection.scalar(text("select clock_timestamp()"))
        row = (
            connection.execute(
                text(
                    """
                select sequence, event_hash, previous_event_hash, payload, occurred_at
                  from artifact_maintenance_event
                 where event_id = :event_id
                """
                ),
                {"event_id": str(REQUEST_ID)},
            )
            .mappings()
            .one()
        )
        head = connection.execute(
            text(
                "select sequence, event_hash from artifact_maintenance_head "
                "where head_id = 1"
            )
        ).one()

    assert before <= recorded.occurred_at <= after
    assert recorded.occurred_at != caller.occurred_at
    assert recorded.event_hash == recorded.recalculate_hash()
    assert row["sequence"] == head.sequence == 1
    assert row["event_hash"] == head.event_hash == recorded.event_hash
    assert row["previous_event_hash"] is None
    assert row["occurred_at"] == recorded.occurred_at
    assert ArtifactMaintenanceAuditEvent.model_validate(row["payload"]) == recorded


def test_requested_and_terminal_form_an_adjacent_verified_chain(
    clean_database: Engine,
) -> None:
    audit = PostgresArtifactMaintenanceAudit(clean_database)
    requested = record_request(clean_database)

    result = audit.record(terminal_for(requested))

    assert isinstance(result, Success)
    terminal = result.value
    assert terminal.previous_event_hash == requested.event_hash
    with clean_database.connect() as connection:
        rows = (
            connection.execute(
                text(
                    """
                select sequence, operation_id, action, phase, content_hash, actor,
                       reason, command_hash, result_hash,
                       previous_event_hash, event_hash
                  from artifact_maintenance_event
                 order by sequence
                """
                )
            )
            .mappings()
            .all()
        )
    assert tuple(row["phase"] for row in rows) == ("requested", "completed")
    assert tuple(row["sequence"] for row in rows) == (1, 2)
    assert rows[1]["previous_event_hash"] == rows[0]["event_hash"]
    for field in (
        "operation_id",
        "action",
        "content_hash",
        "actor",
        "reason",
        "command_hash",
    ):
        assert rows[1][field] == rows[0][field]
    assert rows[0]["result_hash"] is None
    assert rows[1]["result_hash"] == RESULT_HASH


def test_exact_request_and_terminal_retries_return_original_events(
    clean_database: Engine,
) -> None:
    audit = PostgresArtifactMaintenanceAudit(clean_database)
    requested = record_request(clean_database)
    terminal = audit.record(terminal_for(requested))
    assert isinstance(terminal, Success)

    requested_retry = audit.record(
        audit_event(occurred_at=CALLER_TIME + timedelta(days=1))
    )
    terminal_retry = audit.record(
        terminal_for(
            requested,
            event_id=TERMINAL_ID,
        )
    )

    assert requested_retry == Success(requested)
    assert terminal_retry == terminal
    with clean_database.connect() as connection:
        assert (
            connection.scalar(text("select count(*) from artifact_maintenance_event"))
            == 2
        )


def test_same_event_id_with_different_semantics_fails_closed(
    clean_database: Engine,
) -> None:
    record_request(clean_database)

    result = PostgresArtifactMaintenanceAudit(clean_database).record(
        audit_event(reason="different_reason")
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    with clean_database.connect() as connection:
        assert (
            connection.scalar(text("select count(*) from artifact_maintenance_event"))
            == 1
        )


def test_terminal_requires_exact_requested_identity_and_link(
    clean_database: Engine,
) -> None:
    audit = PostgresArtifactMaintenanceAudit(clean_database)
    requested = record_request(clean_database)
    mismatches = (
        terminal_for(requested, actor="operator:other"),
        terminal_for(requested, reason="different_reason"),
        audit_event(
            event_id=TERMINAL_ID,
            operation_id=requested.operation_id,
            action=requested.action,
            phase=ArtifactMaintenancePhase.COMPLETED,
            content_hash=requested.content_hash,
            outcome="restored",
            previous_event_hash="b" * 64,
        ),
    )

    for event in mismatches:
        result = audit.record(event)
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.CONFLICT

    completed = audit.record(terminal_for(requested))
    assert isinstance(completed, Success)


def test_phase_order_and_action_target_shape_fail_closed(
    clean_database: Engine,
) -> None:
    audit = PostgresArtifactMaintenanceAudit(clean_database)
    terminal_without_request = audit.record(
        audit_event(
            event_id=TERMINAL_ID,
            phase=ArtifactMaintenancePhase.FAILED,
            outcome="backend_failed",
            previous_event_hash="b" * 64,
        )
    )
    invalid_gc_target = audit.record(
        audit_event(
            action=ArtifactMaintenanceAction.COLLECT_ORPHANS,
            content_hash=CONTENT_HASH,
        )
    )

    assert isinstance(terminal_without_request, Failure)
    assert terminal_without_request.error.code is ErrorCode.CONFLICT
    assert isinstance(invalid_gc_target, Failure)
    assert invalid_gc_target.error.code is ErrorCode.CONFLICT

    requested = record_request(clean_database)
    second_requested = audit.record(
        audit_event(
            event_id=UUID("a2000000-0000-4000-8000-000000000002"),
            operation_id=UUID("a1000000-0000-4000-8000-000000000002"),
        )
    )
    assert isinstance(second_requested, Failure)
    assert isinstance(audit.record(terminal_for(requested)), Success)

    repeated_operation = audit.record(
        audit_event(event_id=UUID("a2000000-0000-4000-8000-000000000003"))
    )
    missing_restore_target = audit.record(
        audit_event(
            event_id=UUID("a2000000-0000-4000-8000-000000000004"),
            operation_id=UUID("a1000000-0000-4000-8000-000000000004"),
            content_hash=None,
        )
    )
    assert isinstance(repeated_operation, Failure)
    assert isinstance(missing_restore_target, Failure)


def test_concurrent_requests_have_only_one_open_operation(
    clean_database: Engine,
) -> None:
    def submit(index: int):  # type: ignore[no-untyped-def]
        return PostgresArtifactMaintenanceAudit(clean_database).record(
            audit_event(
                event_id=UUID(int=1000 + index),
                operation_id=UUID(int=2000 + index),
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(submit, (1, 2)))

    assert sum(isinstance(result, Success) for result in results) == 1
    assert sum(isinstance(result, Failure) for result in results) == 1
    with clean_database.connect() as connection:
        assert (
            connection.scalar(text("select count(*) from artifact_maintenance_event"))
            == 1
        )


def test_event_is_append_only_and_head_cannot_skip_an_event(
    clean_database: Engine,
) -> None:
    requested = record_request(clean_database)

    for statement in (
        "update artifact_maintenance_event set reason='tampered'",
        "delete from artifact_maintenance_event",
    ):
        with (
            pytest.raises(DBAPIError, match="append-only"),
            clean_database.begin() as connection,
        ):
            connection.execute(text(statement))

    with (
        pytest.raises(DBAPIError, match="has no event"),
        clean_database.begin() as connection,
    ):
        connection.execute(
            text(
                """
                update artifact_maintenance_head
                   set sequence = sequence + 1,
                       event_hash = :event_hash,
                       updated_at = clock_timestamp()
                 where head_id = 1
                """
            ),
            {"event_hash": "f" * 64},
        )

    with clean_database.connect() as connection:
        head = connection.execute(
            text(
                "select sequence, event_hash from artifact_maintenance_head "
                "where head_id=1"
            )
        ).one()
    assert head == (1, requested.event_hash)
