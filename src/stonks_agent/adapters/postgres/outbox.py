"""PostgreSQL SKIP LOCKED transactional outbox delivery."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, or_, select
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.durable_trace import trace_carrier_from_columns
from stonks_agent.adapters.postgres.models import OutboxRow
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.outbox import OutboxAckReceipt, OutboxLease


class PostgresOutbox:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> Result[tuple[OutboxLease, ...]]:
        if not _valid_worker(worker_id) or not _aware(now):
            return _failure(ErrorCode.INVALID_INPUT, "Outbox claim input is invalid")
        if lease_for <= timedelta(0) or not 1 <= limit <= 100:
            return _failure(
                ErrorCode.INVALID_INPUT, "Outbox lease parameters are invalid"
            )
        with Session(self._engine, expire_on_commit=False) as session, session.begin():
            database_now = _database_now(session)
            if database_now is None:
                return _failure(
                    ErrorCode.INTERNAL_ERROR,
                    "Outbox claim database time is invalid",
                )
            rows = session.scalars(
                select(OutboxRow)
                .where(
                    OutboxRow.published_at.is_(None),
                    OutboxRow.not_before <= database_now,
                    OutboxRow.attempts < OutboxRow.max_attempts,
                    or_(
                        OutboxRow.lease_until.is_(None),
                        OutboxRow.lease_until <= database_now,
                    ),
                )
                .order_by(
                    OutboxRow.not_before, OutboxRow.created_at, OutboxRow.outbox_id
                )
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).all()
            for row in rows:
                row.lease_owner = worker_id
                row.lease_until = database_now + lease_for
                row.lease_generation += 1
                row.lease_nonce = uuid4()
                row.attempts += 1
            session.flush()
            return Success(tuple(_lease(row) for row in rows))

    def ack(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        lease_generation: int,
        lease_nonce: UUID,
        now: datetime,
    ) -> Result[OutboxAckReceipt]:
        if (
            not _valid_worker(worker_id)
            or not _valid_fence(lease_generation, lease_nonce)
            or not _aware(now)
        ):
            return _failure(
                ErrorCode.INVALID_INPUT, "Outbox acknowledgement is invalid"
            )
        with Session(self._engine, expire_on_commit=False) as session, session.begin():
            row = session.scalar(
                select(OutboxRow)
                .where(OutboxRow.outbox_id == outbox_id)
                .with_for_update()
            )
            if row is None:
                return _failure(ErrorCode.NOT_FOUND, "Outbox message was not found")
            database_now = _database_now(session)
            if database_now is None:
                return _database_time_failure("Outbox acknowledgement")
            return _ack_locked(
                session,
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                lease_nonce=lease_nonce,
                database_now=database_now,
            )

    def nack(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        lease_generation: int,
        lease_nonce: UUID,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Result[bool]:
        if (
            not _valid_worker(worker_id)
            or not _valid_fence(lease_generation, lease_nonce)
            or not _aware(now)
            or not _aware(retry_at)
            or not error_code.strip()
        ):
            return _failure(ErrorCode.INVALID_INPUT, "Outbox retry input is invalid")
        with Session(self._engine, expire_on_commit=False) as session, session.begin():
            row = session.scalar(
                select(OutboxRow)
                .where(OutboxRow.outbox_id == outbox_id)
                .with_for_update()
            )
            if row is None:
                return _failure(ErrorCode.NOT_FOUND, "Outbox message was not found")
            database_now = _database_now(session)
            if database_now is None:
                return _database_time_failure("Outbox retry")
            if retry_at <= database_now:
                return _failure(
                    ErrorCode.INVALID_INPUT, "Outbox retry input is invalid"
                )
            return _nack_locked(
                session,
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                lease_nonce=lease_nonce,
                database_now=database_now,
                retry_at=retry_at,
                error_code=error_code,
            )


def _ack_locked(
    session: Session,
    row: OutboxRow,
    *,
    worker_id: str,
    lease_generation: int,
    lease_nonce: UUID,
    database_now: datetime,
) -> Result[OutboxAckReceipt]:
    matches = _matches_fence(row, worker_id, lease_generation, lease_nonce)
    if row.published_at is not None:
        if not matches:
            return _failure(ErrorCode.CONFLICT, "Outbox fence mismatch")
        published_at = row.published_at
    else:
        if row.lease_until is None or row.lease_until <= database_now or not matches:
            return _failure(ErrorCode.CONFLICT, "Outbox lease is stale or invalid")
        row.published_at = database_now
        row.lease_until = None
        published_at = database_now
        session.flush()
    return Success(
        OutboxAckReceipt(
            outbox_id=row.outbox_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            lease_nonce=lease_nonce,
            published_at=published_at,
        )
    )


def _nack_locked(
    session: Session,
    row: OutboxRow,
    *,
    worker_id: str,
    lease_generation: int,
    lease_nonce: UUID,
    database_now: datetime,
    retry_at: datetime,
    error_code: str,
) -> Result[bool]:
    invalid = (
        row.published_at is not None
        or not _matches_fence(row, worker_id, lease_generation, lease_nonce)
        or row.lease_until is None
        or row.lease_until <= database_now
    )
    if invalid:
        return _failure(ErrorCode.CONFLICT, "Outbox lease is stale or invalid")
    row.not_before = retry_at
    row.lease_owner = None
    row.lease_until = None
    row.lease_nonce = None
    row.last_error = {"code": error_code[:128]}
    session.flush()
    return Success(True)


def _lease(row: OutboxRow) -> OutboxLease:
    if (
        row.lease_owner is None
        or row.lease_until is None
        or row.lease_nonce is None
        or row.lease_generation < 1
    ):
        raise ValueError("claimed outbox row is missing lease fields")
    return OutboxLease(
        outbox_id=row.outbox_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        sequence=row.sequence,
        topic=row.topic,
        payload=row.payload,
        idempotency_key=row.idempotency_key,
        lease_owner=row.lease_owner,
        lease_until=row.lease_until,
        lease_generation=row.lease_generation,
        lease_nonce=row.lease_nonce,
        attempts=row.attempts,
        trace_carrier=trace_carrier_from_columns(row.traceparent, row.tracestate),
        correlation_id=row.correlation_id,
    )


def _valid_worker(value: str) -> bool:
    return bool(value.strip()) and len(value) <= 128


def _valid_fence(generation: int, nonce: UUID) -> bool:
    return (
        not isinstance(generation, bool) and generation >= 1 and isinstance(nonce, UUID)
    )


def _matches_fence(
    row: OutboxRow,
    worker_id: str,
    generation: int,
    nonce: UUID,
) -> bool:
    return (
        row.lease_owner == worker_id
        and row.lease_generation == generation
        and row.lease_nonce == nonce
    )


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _database_now(session: Session) -> datetime | None:
    value = session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or not _aware(value):
        return None
    return value


def _database_time_failure(operation: str) -> Failure:
    return _failure(
        ErrorCode.INTERNAL_ERROR,
        f"{operation} database time is invalid",
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
