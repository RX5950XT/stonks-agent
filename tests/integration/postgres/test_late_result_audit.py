from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from stonks_agent.adapters.postgres.late_result_audit import PostgresLateResultAudit
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.job import QuarantinedWorkerResult

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 28, 10, tzinfo=UTC)
JOB_ID = UUID("77000000-0000-4000-8000-000000000001")
RUN_ID = UUID("77000000-0000-4000-8000-000000000002")
REQUEST_ID = UUID("77000000-0000-4000-8000-000000000003")


def _record(**overrides: object) -> QuarantinedWorkerResult:
    values: dict[str, object] = {
        "job_id": JOB_ID,
        "run_id": RUN_ID,
        "request_id": REQUEST_ID,
        "attempt_generation": 2,
        "result_artifact_hash": "a" * 64,
        "reason": "stale_attempt",
        "observed_at": NOW,
    }
    values.update(overrides)
    return QuarantinedWorkerResult.model_validate(values)


def test_late_result_audit_is_idempotent_append_only_and_secret_free(
    clean_database: Engine,
) -> None:
    audit = PostgresLateResultAudit(clean_database)
    first = audit.record(_record())
    repeated = audit.record(_record())

    assert first == repeated == Success(_record())
    with clean_database.connect() as connection:
        row = connection.execute(
            text(
                "select reason, record_hash, observed_at from worker_late_result_audit"
            )
        ).one()
        assert row.reason == "stale_attempt"
        assert len(row.record_hash) == 64
        assert row.observed_at == NOW
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "update worker_late_result_audit "
                    "set reason = 'tampered' where job_id = :job_id"
                ),
                {"job_id": JOB_ID},
            )


def test_same_late_result_identity_with_changed_payload_conflicts(
    clean_database: Engine,
) -> None:
    audit = PostgresLateResultAudit(clean_database)
    assert isinstance(audit.record(_record()), Success)

    conflict = audit.record(_record(observed_at=NOW + timedelta(seconds=1)))

    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT
    with clean_database.connect() as connection:
        assert (
            connection.scalar(text("select count(*) from worker_late_result_audit"))
            == 1
        )
