from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.paper_cycle_store import PostgresPaperCycleStore
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.job import EnqueueJob
from stonks_agent.domain.paper_cycle import (
    CancelPaperCycle,
    CanonicalCycleReference,
    PaperCycleRunStatus,
    PaperCycleStage,
    PaperCycleStageOutput,
    RunPaperCycle,
)
from stonks_agent.domain.workflow import CreateWorkflowRun

pytestmark = pytest.mark.postgres

RUN_ID = UUID("47000000-0000-4000-8000-000000000201")
JOB_ID = UUID("47000000-0000-4000-8000-000000000202")
INPUT_HASH = "a" * 64


def test_checkpoint_is_hash_chained_and_stale_generation_cannot_resume(
    clean_database: Engine,
) -> None:
    command = _seed_and_claim(clean_database)
    store = PostgresPaperCycleStore(clean_database)
    loaded = store.load(command)
    assert isinstance(loaded, Success)
    next_state = loaded.value.advance(_output(PaperCycleStage.EVIDENCE, "evidence", 1))

    saved = store.checkpoint(
        command,
        next_state,
        expected_state_hash=loaded.value.state_hash,
    )

    assert isinstance(saved, Success)
    with clean_database.connect() as connection:
        state = connection.execute(
            text("select status, version from run where run_id=:run_id"),
            {"run_id": RUN_ID},
        ).one()
        graph = connection.execute(
            text(
                "select event_type, sequence from run_event "
                "where run_id=:run_id order by sequence"
            ),
            {"run_id": RUN_ID},
        ).all()
        outbox = connection.scalar(
            text("select count(*) from outbox where aggregate_id=:run_id"),
            {"run_id": str(RUN_ID)},
        )
    assert state == ("running", 2)
    assert graph == [("paper_cycle.stage_completed", 2)]
    assert outbox == 1

    with clean_database.begin() as connection:
        connection.execute(
            text(
                "update job set lease_until=clock_timestamp()-interval '1 second' "
                "where job_id=:job_id"
            ),
            {"job_id": JOB_ID},
        )
    reclaimed = PostgresJobQueue(clean_database).claim(
        worker_id="core-runner",
        now=_database_now(clean_database),
        lease_for=timedelta(minutes=5),
    )
    assert isinstance(reclaimed, Success)
    stale = store.load(command)
    resumed = store.load(
        RunPaperCycle(lease=reclaimed.value, cycle_input_hash=INPUT_HASH)
    )
    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT
    assert isinstance(resumed, Success)
    assert resumed.value == next_state


def test_retry_and_dead_letter_are_fenced_audited_transitions(
    clean_database: Engine,
) -> None:
    first = _seed_and_claim(clean_database, max_attempts=2)
    store = PostgresPaperCycleStore(
        clean_database,
        base_retry_delay=timedelta(0),
    )
    retry = store.fail(
        first,
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="provider is temporarily unavailable",
        ),
    )
    assert isinstance(retry, Success)
    assert retry.value.status is PaperCycleRunStatus.RETRY_SCHEDULED
    second_lease = PostgresJobQueue(clean_database).claim(
        worker_id="core-runner",
        now=_database_now(clean_database),
        lease_for=timedelta(minutes=5),
    )
    assert isinstance(second_lease, Success)

    dead = store.fail(
        RunPaperCycle(lease=second_lease.value, cycle_input_hash=INPUT_HASH),
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE,
            message="provider remains unavailable",
        ),
    )

    assert isinstance(dead, Success)
    assert dead.value.status is PaperCycleRunStatus.DEAD_LETTERED
    with clean_database.connect() as connection:
        state = connection.execute(
            text(
                "select j.status, r.status, r.version from job j "
                "join run r on r.run_id=j.run_id where j.job_id=:job_id"
            ),
            {"job_id": JOB_ID},
        ).one()
        events = (
            connection.execute(
                text(
                    "select event_type from run_event "
                    "where run_id=:run_id order by sequence"
                ),
                {"run_id": RUN_ID},
            )
            .scalars()
            .all()
        )
    assert state == ("dead_letter", "failed", 3)
    assert events == [
        "paper_cycle.retry_scheduled",
        "paper_cycle.dead_lettered",
    ]


def test_budget_exhaustion_is_terminal_and_never_schedules_a_chase_retry(
    clean_database: Engine,
) -> None:
    command = _seed_and_claim(clean_database, max_attempts=3)
    store = PostgresPaperCycleStore(clean_database)

    stopped = store.fail(
        command,
        StructuredError(
            code=ErrorCode.BUDGET_EXHAUSTED,
            message="Paper cycle operational budget exhausted",
        ),
    )

    assert isinstance(stopped, Success)
    assert stopped.value.status is PaperCycleRunStatus.DEAD_LETTERED
    with clean_database.connect() as connection:
        row = connection.execute(
            text(
                "select status, not_before, lease_owner from job where job_id=:job_id"
            ),
            {"job_id": JOB_ID},
        ).one()
    assert row.status == "dead_letter"
    assert row.lease_owner is None


def test_cancel_is_terminal_idempotent_and_conflicting_reason_fails_closed(
    clean_database: Engine,
) -> None:
    _seed_run_and_job(clean_database)
    store = PostgresPaperCycleStore(clean_database)
    command = CancelPaperCycle(
        run_id=RUN_ID,
        expected_version=1,
        actor="paper-operator:test",
        reason_code="operator_requested",
    )

    cancelled = store.cancel(command)
    replay = store.cancel(command)
    conflict = store.cancel(command.model_copy(update={"reason_code": "different"}))

    assert isinstance(cancelled, Success)
    assert cancelled.value.status is PaperCycleRunStatus.CANCELLED
    assert replay == cancelled
    assert isinstance(conflict, Failure)
    with clean_database.connect() as connection:
        state = connection.execute(
            text(
                "select j.status, r.status from job j join run r on r.run_id=j.run_id "
                "where j.job_id=:job_id"
            ),
            {"job_id": JOB_ID},
        ).one()
    assert state == ("cancelled", "cancelled")


def test_concurrent_checkpoint_cas_allows_exactly_one_stage_commit(
    clean_database: Engine,
) -> None:
    command = _seed_and_claim(clean_database)
    store = PostgresPaperCycleStore(clean_database)
    loaded = store.load(command)
    assert isinstance(loaded, Success)
    candidate = loaded.value.advance(_output(PaperCycleStage.EVIDENCE, "evidence", 9))
    barrier = Barrier(2)

    def checkpoint() -> object:
        barrier.wait()
        return store.checkpoint(
            command,
            candidate,
            expected_state_hash=loaded.value.state_hash,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: checkpoint(), range(2)))

    assert sum(isinstance(item, Success) for item in results) == 1
    assert sum(isinstance(item, Failure) for item in results) == 1
    with clean_database.connect() as connection:
        assert (
            connection.scalar(
                text("select count(*) from run_event where run_id=:run_id"),
                {"run_id": RUN_ID},
            )
            == 1
        )


def _seed_and_claim(engine: Engine, *, max_attempts: int = 3) -> RunPaperCycle:
    _seed_run_and_job(engine, max_attempts=max_attempts)
    claimed = PostgresJobQueue(engine).claim(
        worker_id="core-runner",
        now=_database_now(engine),
        lease_for=timedelta(minutes=5),
    )
    assert isinstance(claimed, Success)
    return RunPaperCycle(lease=claimed.value, cycle_input_hash=INPUT_HASH)


def _seed_run_and_job(engine: Engine, *, max_attempts: int = 3) -> None:
    now = _database_now(engine)
    with PostgresUnitOfWork(engine) as transaction:
        created = transaction.workflows.create(
            CreateWorkflowRun(
                run_id=RUN_ID,
                run_type="paper_fund_cycle",
                as_of=now,
                policy_id="paper-fund-cycle/1.0.0",
                idempotency_key="paper-cycle:test",
                input_hash=INPUT_HASH,
                owner_subject="system:paper-cycle",
                created_at=now,
            )
        )
        assert isinstance(created, Success)
        transaction.commit()
    queued = PostgresJobQueue(engine).enqueue(
        EnqueueJob(
            job_id=JOB_ID,
            run_id=RUN_ID,
            job_type="paper_fund_cycle",
            payload={"cycle_input_hash": INPUT_HASH},
            idempotency_key="paper-cycle:test:job",
            not_before=now,
            deadline_at=now + timedelta(hours=1),
            max_attempts=max_attempts,
            created_at=now,
        )
    )
    assert isinstance(queued, Success)


def _database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        value = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(value, datetime)
    return value


def _output(
    stage: PaperCycleStage,
    ref_type: str,
    suffix: int,
) -> PaperCycleStageOutput:
    return PaperCycleStageOutput.create(
        stage=stage,
        references=(
            CanonicalCycleReference(
                ref_type=ref_type,
                ref_id=f"paper-cycle-ref-{suffix}",
                content_hash=f"{suffix:064x}",
            ),
        ),
    )
