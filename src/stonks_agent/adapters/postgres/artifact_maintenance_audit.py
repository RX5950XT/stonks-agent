"""PostgreSQL-owned artifact maintenance audit chain."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    ArtifactMaintenanceEventRow,
    ArtifactMaintenanceHeadRow,
)
from stonks_agent.domain.artifact_retention import (
    ArtifactMaintenanceAction,
    ArtifactMaintenanceAuditEvent,
    ArtifactMaintenancePhase,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)


class PostgresArtifactMaintenanceAudit:
    """Serialize audit mutations with database time and an append-only hash chain."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(
        self, event: ArtifactMaintenanceAuditEvent
    ) -> Result[ArtifactMaintenanceAuditEvent]:
        try:
            with (
                Session(self._engine, expire_on_commit=False) as session,
                session.begin(),
            ):
                result = _record_locked(session, event)
                if isinstance(result, Failure):
                    session.rollback()
                return result
        except IntegrityError:
            return _failure(
                ErrorCode.CONFLICT,
                "Artifact maintenance audit conflicted",
            )
        except (ValidationError, ValueError):
            return _failure(
                ErrorCode.CONFLICT,
                "Artifact maintenance audit is invalid",
            )
        except SQLAlchemyError:
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Artifact maintenance audit failed",
            )


def _record_locked(
    session: Session,
    event: ArtifactMaintenanceAuditEvent,
) -> Result[ArtifactMaintenanceAuditEvent]:
    head = session.scalar(
        select(ArtifactMaintenanceHeadRow)
        .where(ArtifactMaintenanceHeadRow.head_id == 1)
        .with_for_update()
    )
    if head is None:
        return _failure(ErrorCode.CONFLICT, "Artifact maintenance head is missing")
    existing = session.get(ArtifactMaintenanceEventRow, event.event_id)
    if existing is not None:
        return _retry(existing, event)
    last = _last_event(session, head)
    if not _authorized(event, last, session):
        return _failure(
            ErrorCode.CONFLICT, "Artifact maintenance event is out of order"
        )
    occurred_at = session.scalar(select(func.clock_timestamp()))
    if not isinstance(occurred_at, datetime):
        return _failure(
            ErrorCode.INTERNAL_ERROR,
            "Artifact maintenance database time is invalid",
        )
    recorded = ArtifactMaintenanceAuditEvent.create(
        event_id=event.event_id,
        operation_id=event.operation_id,
        action=event.action,
        phase=event.phase,
        content_hash=event.content_hash,
        actor=event.actor,
        reason=event.reason,
        command_hash=event.command_hash,
        result_hash=event.result_hash,
        occurred_at=occurred_at,
        outcome=event.outcome,
        previous_event_hash=head.event_hash,
    )
    sequence = head.sequence + 1
    session.add(_event_row(recorded, sequence))
    session.flush()
    head.sequence = sequence
    head.event_hash = recorded.event_hash
    head.updated_at = occurred_at
    session.flush()
    return Success(recorded)


def _last_event(
    session: Session,
    head: ArtifactMaintenanceHeadRow,
) -> ArtifactMaintenanceAuditEvent | None:
    if head.sequence == 0:
        return None
    row = session.scalar(
        select(ArtifactMaintenanceEventRow).where(
            ArtifactMaintenanceEventRow.sequence == head.sequence
        )
    )
    if row is None:
        raise ValueError("artifact maintenance head has no event")
    event = _stored_event(row)
    if event.event_hash != head.event_hash:
        raise ValueError("artifact maintenance head hash mismatches")
    return event


def _authorized(
    event: ArtifactMaintenanceAuditEvent,
    last: ArtifactMaintenanceAuditEvent | None,
    session: Session,
) -> bool:
    if not _target_shape(event):
        return False
    if event.phase is ArtifactMaintenancePhase.REQUESTED:
        duplicate = session.scalar(
            select(ArtifactMaintenanceEventRow.event_id).where(
                ArtifactMaintenanceEventRow.operation_id == event.operation_id
            )
        )
        return (
            event.previous_event_hash is None
            and duplicate is None
            and (
                last is None
                or last.phase
                in {
                    ArtifactMaintenancePhase.COMPLETED,
                    ArtifactMaintenancePhase.FAILED,
                }
            )
        )
    return (
        last is not None
        and last.phase is ArtifactMaintenancePhase.REQUESTED
        and event.previous_event_hash == last.event_hash
        and _same_operation(last, event)
    )


def _target_shape(event: ArtifactMaintenanceAuditEvent) -> bool:
    return (event.action is ArtifactMaintenanceAction.COLLECT_ORPHANS) == (
        event.content_hash is None
    )


def _same_operation(
    requested: ArtifactMaintenanceAuditEvent,
    terminal: ArtifactMaintenanceAuditEvent,
) -> bool:
    return (
        terminal.operation_id == requested.operation_id
        and terminal.action is requested.action
        and terminal.content_hash == requested.content_hash
        and terminal.actor == requested.actor
        and terminal.reason == requested.reason
        and terminal.command_hash == requested.command_hash
    )


def _retry(
    row: ArtifactMaintenanceEventRow,
    event: ArtifactMaintenanceAuditEvent,
) -> Result[ArtifactMaintenanceAuditEvent]:
    stored = _stored_event(row)
    same = (
        stored.event_id == event.event_id
        and stored.operation_id == event.operation_id
        and stored.action is event.action
        and stored.phase is event.phase
        and stored.content_hash == event.content_hash
        and stored.actor == event.actor
        and stored.reason == event.reason
        and stored.command_hash == event.command_hash
        and stored.result_hash == event.result_hash
        and stored.outcome == event.outcome
        and (
            event.phase is ArtifactMaintenancePhase.REQUESTED
            or stored.previous_event_hash == event.previous_event_hash
        )
    )
    if not same:
        return _failure(ErrorCode.CONFLICT, "Artifact maintenance retry conflicts")
    return Success(stored)


def _stored_event(row: ArtifactMaintenanceEventRow) -> ArtifactMaintenanceAuditEvent:
    event = ArtifactMaintenanceAuditEvent.model_validate(row.payload)
    if (
        event.event_id != row.event_id
        or event.operation_id != row.operation_id
        or event.action.value != row.action
        or event.phase.value != row.phase
        or event.content_hash != row.content_hash
        or event.actor != row.actor
        or event.reason != row.reason
        or event.command_hash != row.command_hash
        or event.result_hash != row.result_hash
        or event.outcome != row.outcome
        or event.previous_event_hash != row.previous_event_hash
        or event.event_hash != row.event_hash
        or event.occurred_at != row.occurred_at
    ):
        raise ValueError("artifact maintenance stored event is corrupt")
    return event


def _event_row(
    event: ArtifactMaintenanceAuditEvent,
    sequence: int,
) -> ArtifactMaintenanceEventRow:
    return ArtifactMaintenanceEventRow(
        event_id=event.event_id,
        operation_id=event.operation_id,
        sequence=sequence,
        action=event.action.value,
        phase=event.phase.value,
        content_hash=event.content_hash,
        actor=event.actor,
        reason=event.reason,
        command_hash=event.command_hash,
        result_hash=event.result_hash,
        outcome=event.outcome,
        previous_event_hash=event.previous_event_hash,
        event_hash=event.event_hash,
        payload=event.model_dump(mode="json"),
        occurred_at=event.occurred_at,
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
