from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import pytest
from integration.postgres.test_paper_execution import (
    ACCOUNT_ID,
    _broker,
    _ledger_policy,
    _seed_order,
    execution_request,
)
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from support.budgets import FixedBudgetEvaluator
from support.paper_cycle import paper_cycle_input, paper_cycle_payload

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.ledger_repository import PostgresLedgerRepository
from stonks_agent.adapters.postgres.paper_cycle_store import PostgresPaperCycleStore
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.application.execution.execute import execute_reference_paper
from stonks_agent.application.ledger.reconcile import reconcile_paper_account
from stonks_agent.application.workflows.run_cycle import run_paper_fund_cycle
from stonks_agent.domain.errors import Result, Success
from stonks_agent.domain.job import EnqueueJob
from stonks_agent.domain.paper_cycle import (
    CanonicalCycleReference,
    PaperCycleRunStatus,
    PaperCycleStage,
    PaperCycleStageOutput,
    PaperCycleState,
    RunPaperCycle,
)
from stonks_agent.domain.workflow import CreateWorkflowRun
from stonks_contracts.common import stable_payload_hash

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]
pytest_plugins = ["integration.postgres.conftest"]

JOB_ID = UUID("47000000-0000-4000-8000-000000000301")


class CrashAfterReceiptCommit(BaseException):
    pass


class CanonicalPaperCycleHandler:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._request = execution_request()
        self._crashed = False
        self.execution_attempts = 0

    def advance(
        self,
        command: RunPaperCycle,
        stage: PaperCycleStage,
        state: PaperCycleState,
    ) -> Result[PaperCycleStageOutput]:
        assert command.cycle_input.run_id == state.run_id
        if stage is PaperCycleStage.EXECUTION_RECEIPT:
            return self._execute_then_crash_once()
        if stage is PaperCycleStage.LEDGER:
            return self._ledger(state)
        return Success(self._reference_stage(stage, state))

    def _execute_then_crash_once(self) -> Result[PaperCycleStageOutput]:
        self.execution_attempts += 1
        executed = execute_reference_paper(
            self._request,
            _broker(),
            _ledger_policy(),
            lambda: PostgresUnitOfWork(self._engine),
        )
        if not isinstance(executed, Success):
            return executed
        receipt = executed.value.outcome.receipt
        if not self._crashed:
            self._crashed = True
            raise CrashAfterReceiptCommit
        return Success(
            PaperCycleStageOutput.create(
                stage=PaperCycleStage.EXECUTION_RECEIPT,
                references=(
                    CanonicalCycleReference(
                        ref_type="execution_receipt",
                        ref_id=str(receipt.receipt_id),
                        content_hash=receipt.receipt_hash,
                    ),
                ),
            )
        )

    def _ledger(self, state: PaperCycleState) -> Result[PaperCycleStageOutput]:
        reconciled = reconcile_paper_account(
            ACCOUNT_ID,
            as_of=self._request.as_of,
            unit_of_work=lambda: PostgresUnitOfWork(self._engine),
        )
        if not isinstance(reconciled, Success):
            return reconciled
        with Session(self._engine) as session:
            projection = PostgresLedgerRepository(session).get_projection(ACCOUNT_ID)
        if not isinstance(projection, Success):
            return projection
        return Success(
            PaperCycleStageOutput.create(
                stage=PaperCycleStage.LEDGER,
                references=(
                    CanonicalCycleReference(
                        ref_type="ledger_projection",
                        ref_id=(
                            f"{ACCOUNT_ID}:ledger:{projection.value.ledger_sequence}"
                        ),
                        content_hash=projection.value.projection_hash,
                    ),
                ),
            )
        )

    def _reference_stage(
        self,
        stage: PaperCycleStage,
        state: PaperCycleState,
    ) -> PaperCycleStageOutput:
        intent = self._request.command.intent
        if stage is PaperCycleStage.PORTFOLIO_TARGET:
            references = (
                CanonicalCycleReference(
                    ref_type="portfolio_target",
                    ref_id=str(intent.portfolio_target_id),
                    content_hash=intent.authorized_target_hash,
                ),
            )
        elif stage is PaperCycleStage.RISK_DECISION:
            references = (
                CanonicalCycleReference(
                    ref_type="risk_decision",
                    ref_id=str(intent.risk_decision_id),
                    content_hash=intent.risk_decision_hash,
                ),
            )
        elif stage is PaperCycleStage.ORDER_INTENT:
            references = (
                CanonicalCycleReference(
                    ref_type="order_intent",
                    ref_id=str(intent.intent_id),
                    content_hash=intent.intent_hash,
                ),
            )
        else:
            references = _research_reference(stage, state)
        return PaperCycleStageOutput.create(stage=stage, references=references)


def test_postgres_cycle_execution_crash_reuses_receipt_and_completes_graph(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    first = _seed_cycle_job(clean_database)
    store = PostgresPaperCycleStore(clean_database)
    handler = CanonicalPaperCycleHandler(clean_database)
    artifacts = MemoryArtifactStore()

    with pytest.raises(CrashAfterReceiptCommit):
        run_paper_fund_cycle(
            first,
            handler=handler,
            store=store,
            artifacts=artifacts,
            budget=FixedBudgetEvaluator(),
            clock=lambda: execution_request().as_of,
        )
    with clean_database.begin() as connection:
        connection.execute(
            text(
                "update job set lease_until=clock_timestamp()-interval '1 second' "
                "where job_id=:job_id"
            ),
            {"job_id": JOB_ID},
        )
    reclaimed = PostgresJobQueue(clean_database).claim(
        worker_id="core-cycle-runner",
        now=_database_now(clean_database),
        lease_for=timedelta(minutes=5),
    )
    assert isinstance(reclaimed, Success)

    completed = run_paper_fund_cycle(
        RunPaperCycle(lease=reclaimed.value),
        handler=handler,
        store=store,
        artifacts=artifacts,
        budget=FixedBudgetEvaluator(),
        clock=lambda: execution_request().as_of,
    )

    assert isinstance(completed, Success)
    assert completed.value.status is PaperCycleRunStatus.SUCCEEDED
    assert handler.execution_attempts == 2
    assert completed.value.result_artifact_hash is not None
    assert artifacts.is_finalized(completed.value.result_artifact_hash)
    exact_replay = run_paper_fund_cycle(
        RunPaperCycle(lease=reclaimed.value),
        handler=handler,
        store=store,
        artifacts=artifacts,
        budget=FixedBudgetEvaluator(),
        clock=lambda: execution_request().as_of,
    )
    assert exact_replay == completed
    assert handler.execution_attempts == 2
    with clean_database.connect() as connection:
        counts = (
            connection.execute(
                text(
                    """
                select
                  (select count(*) from paper_execution_receipt) receipts,
                  (select count(*) from paper_fill) fills,
                  (select count(*) from journal_transaction) journals,
                  (select count(*) from run_event where run_id=:run_id) events,
                  (select count(*) from outbox where aggregate_id=:run_id_text) outbox
                """
                ),
                {
                    "run_id": first.lease.run_id,
                    "run_id_text": str(first.lease.run_id),
                },
            )
            .mappings()
            .one()
        )
        run_status = connection.execute(
            text(
                "select r.status run_status, j.status job_status "
                "from run r join job j on j.run_id=r.run_id "
                "where r.run_id=:run_id"
            ),
            {"run_id": first.lease.run_id},
        ).one()
    assert counts == {
        "receipts": 1,
        "fills": 1,
        "journals": 1,
        "events": 10,
        "outbox": 10,
    }
    assert run_status == ("succeeded", "succeeded")


def _seed_cycle_job(engine: Engine) -> RunPaperCycle:
    now = _database_now(engine)
    run_id = execution_request().command.intent.run_id
    deadline_at = now + timedelta(hours=1)
    cycle_input = paper_cycle_input(
        run_id=run_id,
        as_of=execution_request().as_of,
        deadline_at=deadline_at,
    )
    input_hash = cycle_input.cycle_input_hash
    with PostgresUnitOfWork(engine) as transaction:
        created = transaction.workflows.create(
            CreateWorkflowRun(
                run_id=run_id,
                run_type="paper_fund_cycle",
                as_of=execution_request().as_of,
                policy_id="paper-fund-cycle/1.0.0",
                idempotency_key="paper-cycle:e2e",
                input_hash=input_hash,
                owner_subject="system:paper-cycle",
                created_at=now,
            )
        )
        assert isinstance(created, Success)
        transaction.commit()
    queued = PostgresJobQueue(engine).enqueue(
        EnqueueJob(
            job_id=JOB_ID,
            run_id=run_id,
            job_type="paper_fund_cycle",
            payload=paper_cycle_payload(cycle_input),
            idempotency_key="paper-cycle:e2e:job",
            not_before=now,
            deadline_at=deadline_at,
            max_attempts=3,
            created_at=now,
        )
    )
    assert isinstance(queued, Success)
    claimed = PostgresJobQueue(engine).claim(
        worker_id="core-cycle-runner",
        now=now,
        lease_for=timedelta(minutes=5),
    )
    assert isinstance(claimed, Success)
    return RunPaperCycle(lease=claimed.value)


def _research_reference(
    stage: PaperCycleStage,
    state: PaperCycleState,
) -> tuple[CanonicalCycleReference, ...]:
    mapping = {
        PaperCycleStage.EVIDENCE: "evidence",
        PaperCycleStage.RESEARCH_OPINION: "research_artifact",
        PaperCycleStage.SIGNAL: "alpha_signal",
        PaperCycleStage.REPORT: "analysis_report",
    }
    ref_type = mapping[stage]
    index = tuple(PaperCycleStage).index(stage) + 1
    return (
        CanonicalCycleReference(
            ref_type=ref_type,
            ref_id=f"paper-cycle-e2e:{ref_type}:{index}",
            content_hash=stable_payload_hash(
                {
                    "run_id": str(state.run_id),
                    "stage": stage.value,
                    "previous_state_hash": state.state_hash,
                }
            ),
        ),
    )


def _database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        value = connection.scalar(text("select clock_timestamp()"))
    assert isinstance(value, datetime)
    return value
