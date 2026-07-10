"""PostgreSQL SKIP LOCKED transactional outbox delivery."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

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
            return _failure(ErrorCode.INVALID_INPUT, "Outbox lease parameters are invalid")
        with Session(self._engine, expire_on_commit=False) as session, session.begin():
            rows = session.scalars(
                select(OutboxRow)
                .where(
                    OutboxRow.published_at.is_(None),
                    OutboxRow.not_before <= now,
                    OutboxRow.attempts < OutboxRow.max_attempts,
                    or_(
                        OutboxRow.lease_until.is_(None),
                        OutboxRow.lease_until <= now,
                    ),
                )
                .order_by(OutboxRow.not_before, OutboxRow.created_at, OutboxRow.outbox_id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).all()
            for row in rows:
                row.lease_owner = worker_id
                row.lease_until = now + lease_for
                row.attempts += 1
            session.flush()
            return Success(tuple(_lease(row) for row in rows))

    def ack(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        now: datetime,
    ) -> Result[OutboxAckReceipt]:
        if not _valid_worker(worker_id) or not _aware(now):
            return _failure(ErrorCode.INVALID_INPUT, "Outbox acknowledgement is invalid")
        with Session(self._engine, expire_on_commit=False) as session, session.begin():
            row = session.scalar(
                select(OutboxRow)
                .where(OutboxRow.outbox_id == outbox_id)
                .with_for_update()
            )
            if row is None:
                return _failure(ErrorCode.NOT_FOUND, "Outbox message was not found")
            if row.published_at is not None:
                if row.lease_owner != worker_id:
                    return _failure(ErrorCode.CONFLICT, "Outbox owner mismatch")
                return Success(
                    OutboxAckReceipt(
                        outbox_id=row.outbox_id,
                        worker_id=worker_id,
                        published_at=row.published_at,
                    )
                )
            if (
                row.lease_owner != worker_id
                or row.lease_until is None
                or row.lease_until <= now
            ):
                return _failure(ErrorCode.CONFLICT, "Outbox lease is stale or invalid")
            row.published_at = now
            row.lease_until = None
            session.flush()
            return Success(
                OutboxAckReceipt(
                    outbox_id=row.outbox_id,
                    worker_id=worker_id,
                    published_at=now,
                )
            )

    def nack(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Result[bool]:
        if (
            not _valid_worker(worker_id)
            or not _aware(now)
            or not _aware(retry_at)
            or retry_at <= now
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
            if (
                row.published_at is not None
                or row.lease_owner != worker_id
                or row.lease_until is None
                or row.lease_until <= now
            ):
                return _failure(ErrorCode.CONFLICT, "Outbox lease is stale or invalid")
            row.not_before = retry_at
            row.lease_owner = None
            row.lease_until = None
            row.last_error = {"code": error_code[:128]}
            session.flush()
            return Success(True)


def _lease(row: OutboxRow) -> OutboxLease:
    if row.lease_owner is None or row.lease_until is None:
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
        attempts=row.attempts,
    )


def _valid_worker(value: str) -> bool:
    return bool(value.strip()) and len(value) <= 128


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
