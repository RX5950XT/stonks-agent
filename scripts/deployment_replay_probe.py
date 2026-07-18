#!/usr/bin/env python3
"""Prove deterministic workflow persistence and replay across core restarts."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine

from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.config.deployment import load_deployment_settings
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.workflow import (
    CreateWorkflowRun,
    WorkflowRunRecord,
    WorkflowStatus,
)
from stonks_agent.entrypoints.deployment import create_runtime_engine
from stonks_contracts.common import canonical_json, stable_payload_hash

_RUN_ID = UUID("74000000-0000-4000-8000-000000000067")
_AS_OF = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)
_CREATED_AT = datetime(2026, 7, 18, 0, 0, 1, tzinfo=UTC)
_RUNNING_AT = datetime(2026, 7, 18, 0, 0, 2, tzinfo=UTC)
_SUCCEEDED_AT = datetime(2026, 7, 18, 0, 0, 3, tzinfo=UTC)
_IDEMPOTENCY_KEY = "deployment-replay:v1"
_OWNER_SUBJECT = "system:deployment-replay"
_REPLAY_ARTIFACT = {
    "as_of": "2026-07-18T00:00:00Z",
    "claim": "deterministic_persistence_replay_only",
    "fresh_inference": False,
    "schema": "stonks/deployment-replay/1",
}
_ARTIFACT_HASH = stable_payload_hash(_REPLAY_ARTIFACT)


class DeploymentReplayProbeError(RuntimeError):
    """Public-safe failure for the deployment replay smoke boundary."""

    def __init__(self) -> None:
        super().__init__("Deployment replay probe failed")


class _WorkflowStore(Protocol):
    def create(self, request: CreateWorkflowRun) -> Result[WorkflowRunRecord]: ...

    def get(self, run_id: UUID) -> Result[WorkflowRunRecord]: ...

    def transition(
        self,
        run_id: UUID,
        *,
        expected_version: int,
        new_status: WorkflowStatus,
        updated_at: datetime,
    ) -> Result[WorkflowRunRecord]: ...


class _WorkflowUnitOfWork(Protocol):
    @property
    def workflows(self) -> _WorkflowStore: ...

    def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], AbstractContextManager[_WorkflowUnitOfWork]]


def replay_request() -> CreateWorkflowRun:
    """Return the fixed immutable request bound to the replay artifact hash."""

    return CreateWorkflowRun(
        run_id=_RUN_ID,
        run_type="deployment_replay",
        as_of=_AS_OF,
        policy_id="deployment-replay/v1",
        idempotency_key=_IDEMPOTENCY_KEY,
        input_hash=_ARTIFACT_HASH,
        owner_subject=_OWNER_SUBJECT,
        created_at=_CREATED_AT,
    )


def execute_stage(stage: str, unit_of_work: UnitOfWorkFactory) -> str:
    """Execute one idempotent stage and return canonical, non-secret JSON."""

    try:
        if stage == "write":
            record = _write(unit_of_work)
        elif stage == "replay":
            record = _replay(unit_of_work)
        elif stage == "verify":
            record = _verify(unit_of_work)
        else:
            raise ValueError("unknown replay stage")
        return _canonical_output(stage, record)
    except DeploymentReplayProbeError:
        raise
    except Exception as error:
        raise DeploymentReplayProbeError() from error


def execute_stage_with_engine(stage: str, engine: Engine) -> str:
    return execute_stage(stage, lambda: PostgresUnitOfWork(engine))


def main(
    argv: Sequence[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    source = os.environ if environment is None else environment
    engine: Engine | None = None
    try:
        if len(arguments) != 1:
            raise DeploymentReplayProbeError()
        settings = load_deployment_settings(source)
        engine = create_runtime_engine(settings)
        print(execute_stage_with_engine(arguments[0], engine))
        return 0
    except Exception:
        print(
            canonical_json(
                {
                    "error": {
                        "code": "deployment_replay_failed",
                        "message": "Deployment replay probe failed",
                    },
                    "success": False,
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def _write(unit_of_work: UnitOfWorkFactory) -> WorkflowRunRecord:
    request = replay_request()
    with unit_of_work() as transaction:
        current = _successful_record(transaction.workflows.create(request))
        _validate_request(current, request)
        if _has_state(current, WorkflowStatus.RUNNING, 2, _RUNNING_AT):
            return current
        if not _has_state(current, WorkflowStatus.PENDING, 1, _CREATED_AT):
            raise DeploymentReplayProbeError()
        transitioned = _successful_record(
            transaction.workflows.transition(
                request.run_id,
                expected_version=1,
                new_status=WorkflowStatus.RUNNING,
                updated_at=_RUNNING_AT,
            )
        )
        _validate_exact_state(
            transitioned,
            request,
            status=WorkflowStatus.RUNNING,
            version=2,
            updated_at=_RUNNING_AT,
        )
        transaction.commit()
        return transitioned


def _replay(unit_of_work: UnitOfWorkFactory) -> WorkflowRunRecord:
    request = replay_request()
    with unit_of_work() as transaction:
        current = _successful_record(transaction.workflows.get(request.run_id))
        _validate_request(current, request)
        if _has_state(current, WorkflowStatus.SUCCEEDED, 3, _SUCCEEDED_AT):
            return current
        if not _has_state(current, WorkflowStatus.RUNNING, 2, _RUNNING_AT):
            raise DeploymentReplayProbeError()
        transitioned = _successful_record(
            transaction.workflows.transition(
                request.run_id,
                expected_version=2,
                new_status=WorkflowStatus.SUCCEEDED,
                updated_at=_SUCCEEDED_AT,
            )
        )
        _validate_exact_state(
            transitioned,
            request,
            status=WorkflowStatus.SUCCEEDED,
            version=3,
            updated_at=_SUCCEEDED_AT,
        )
        transaction.commit()
        return transitioned


def _verify(unit_of_work: UnitOfWorkFactory) -> WorkflowRunRecord:
    request = replay_request()
    with unit_of_work() as transaction:
        current = _successful_record(transaction.workflows.get(request.run_id))
        _validate_exact_state(
            current,
            request,
            status=WorkflowStatus.SUCCEEDED,
            version=3,
            updated_at=_SUCCEEDED_AT,
        )
        return current


def _successful_record(
    result: Result[WorkflowRunRecord],
) -> WorkflowRunRecord:
    if isinstance(result, Failure):
        raise DeploymentReplayProbeError()
    return result.value


def _validate_request(
    record: WorkflowRunRecord,
    request: CreateWorkflowRun,
) -> None:
    actual = record.model_dump(
        include=set(CreateWorkflowRun.model_fields),
        mode="python",
    )
    if actual != request.model_dump(mode="python"):
        raise DeploymentReplayProbeError()


def _has_state(
    record: WorkflowRunRecord,
    status: WorkflowStatus,
    version: int,
    updated_at: datetime,
) -> bool:
    return (
        record.status is status
        and record.version == version
        and record.updated_at == updated_at
    )


def _validate_exact_state(
    record: WorkflowRunRecord,
    request: CreateWorkflowRun,
    *,
    status: WorkflowStatus,
    version: int,
    updated_at: datetime,
) -> None:
    _validate_request(record, request)
    if not _has_state(record, status, version, updated_at):
        raise DeploymentReplayProbeError()


def _canonical_output(stage: str, record: WorkflowRunRecord) -> str:
    payload = {
        "artifact_hash": record.input_hash,
        "fresh_inference": False,
        "replay_source": "persisted_workflow_record",
        "run_id": str(record.run_id),
        "stage": stage,
        "status": record.status.value,
        "success": True,
        "version": record.version,
    }
    return canonical_json(
        {
            **payload,
            "output_hash": stable_payload_hash(payload),
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
