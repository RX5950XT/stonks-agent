"""Append-only PostgreSQL quarantine audit for stale worker results."""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import WorkerLateResultAuditRow
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import QuarantinedWorkerResult
from stonks_contracts.common import stable_payload_hash


class PostgresLateResultAudit:
    """Persist a secret-free immutable record without canonical commit authority."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(
        self,
        result: QuarantinedWorkerResult,
    ) -> Result[QuarantinedWorkerResult]:
        try:
            with (
                Session(self._engine, expire_on_commit=False) as session,
                session.begin(),
            ):
                existing = _existing(session, result)
                if existing is not None:
                    return _validate_existing(existing, result)
                session.add(_row(result))
                session.flush()
                return Success(result)
        except IntegrityError:
            return _existing_after_race(self._engine, result)
        except SQLAlchemyError:
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Late worker result audit failed",
            )


def _row(result: QuarantinedWorkerResult) -> WorkerLateResultAuditRow:
    return WorkerLateResultAuditRow(
        audit_id=_audit_id(result),
        job_id=result.job_id,
        run_id=result.run_id,
        request_id=result.request_id,
        attempt_generation=result.attempt_generation,
        result_artifact_hash=result.result_artifact_hash,
        reason=str(result.reason),
        record_hash=stable_payload_hash(result.model_dump(mode="json")),
        observed_at=result.observed_at,
    )


def _existing(
    session: Session,
    result: QuarantinedWorkerResult,
) -> WorkerLateResultAuditRow | None:
    return session.scalar(
        select(WorkerLateResultAuditRow)
        .where(WorkerLateResultAuditRow.audit_id == _audit_id(result))
        .with_for_update()
    )


def _validate_existing(
    row: WorkerLateResultAuditRow,
    result: QuarantinedWorkerResult,
) -> Result[QuarantinedWorkerResult]:
    expected_hash = stable_payload_hash(result.model_dump(mode="json"))
    if (
        row.job_id != result.job_id
        or row.run_id != result.run_id
        or row.request_id != result.request_id
        or row.attempt_generation != result.attempt_generation
        or row.result_artifact_hash != result.result_artifact_hash
        or row.reason != result.reason
        or row.observed_at != result.observed_at
        or row.record_hash != expected_hash
    ):
        return _failure(ErrorCode.CONFLICT, "Late worker result audit conflicts")
    return Success(result)


def _existing_after_race(
    engine: Engine,
    result: QuarantinedWorkerResult,
) -> Result[QuarantinedWorkerResult]:
    try:
        with Session(engine) as session:
            existing = _existing(session, result)
            if existing is None:
                return _failure(
                    ErrorCode.CONFLICT, "Late worker result audit conflicts"
                )
            return _validate_existing(existing, result)
    except SQLAlchemyError:
        return _failure(ErrorCode.INTERNAL_ERROR, "Late worker result audit failed")


def _audit_id(result: QuarantinedWorkerResult) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "stonks:worker-late-result:"
        f"{result.job_id}:{result.attempt_generation}:"
        f"{result.result_artifact_hash}:{result.reason}",
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
