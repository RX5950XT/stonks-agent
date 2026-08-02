from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from support.budgets import FixedBudgetEvaluator
from support.paper_cycle import paper_cycle_input, paper_cycle_payload
from support.telemetry import RecordingOperationRecorder

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.workflows.run_cycle import (
    cancel_paper_fund_cycle,
    run_paper_fund_cycle,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.operational_budget import BudgetStatus
from stonks_agent.domain.paper_cycle import (
    CancelPaperCycle,
    CanonicalCycleReference,
    PaperCycleRunResult,
    PaperCycleRunStatus,
    PaperCycleStage,
    PaperCycleStageOutput,
    PaperCycleState,
    RunPaperCycle,
)
from stonks_agent.domain.telemetry import ComponentName, OperationName
from stonks_agent.ports.artifact_store import ArtifactManifest

NOW = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)
RUN_ID = UUID("47000000-0000-4000-8000-000000000101")
JOB_ID = UUID("47000000-0000-4000-8000-000000000102")
CYCLE_DEADLINE = NOW + timedelta(hours=1)
CYCLE_INPUT = paper_cycle_input(
    run_id=RUN_ID,
    as_of=NOW,
    deadline_at=CYCLE_DEADLINE,
)

REFERENCE_TYPES = {
    PaperCycleStage.EVIDENCE: "evidence",
    PaperCycleStage.RESEARCH_OPINION: "research_artifact",
    PaperCycleStage.SIGNAL: "alpha_signal",
    PaperCycleStage.PORTFOLIO_TARGET: "portfolio_target",
    PaperCycleStage.RISK_DECISION: "risk_decision",
    PaperCycleStage.ORDER_INTENT: "order_intent",
    PaperCycleStage.EXECUTION_RECEIPT: "execution_receipt",
    PaperCycleStage.LEDGER: "ledger_projection",
    PaperCycleStage.REPORT: "analysis_report",
}


def lease(*, generation: int = 1) -> JobLease:
    return JobLease(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="paper_fund_cycle",
        payload=paper_cycle_payload(CYCLE_INPUT),
        attempt_generation=generation,
        attempt_nonce=f"attempt-{generation}",
        lease_owner="core-runner",
        lease_until=NOW + timedelta(minutes=5),
        attempts=generation,
        deadline_at=CYCLE_DEADLINE,
    )


def request(*, generation: int = 1) -> RunPaperCycle:
    return RunPaperCycle(lease=lease(generation=generation))


def stage_output(stage: PaperCycleStage) -> PaperCycleStageOutput:
    index = tuple(PaperCycleStage).index(stage) + 1
    return PaperCycleStageOutput.create(
        stage=stage,
        references=(
            CanonicalCycleReference(
                ref_type=REFERENCE_TYPES[stage],
                ref_id=f"cycle-ref-{index}",
                content_hash=f"{index:064x}",
            ),
        ),
    )


class FakeHandler:
    def __init__(self, *, fail_at: PaperCycleStage | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[PaperCycleStage] = []

    def advance(
        self,
        command: RunPaperCycle,
        stage: PaperCycleStage,
        state: PaperCycleState,
    ) -> Result[PaperCycleStageOutput]:
        assert command.cycle_input.run_id == RUN_ID
        self.calls.append(stage)
        if stage is self.fail_at:
            return Failure(
                StructuredError(
                    code=ErrorCode.DATA_UNAVAILABLE,
                    message="transient stage outage",
                )
            )
        return Success(stage_output(stage))


class SimulatedCrash(BaseException):
    pass


class CrashAfterExecutionHandler(FakeHandler):
    def __init__(self) -> None:
        super().__init__()
        self.execution_side_effects = 0
        self.receipt_exists = False
        self.crashed = False

    def advance(
        self,
        command: RunPaperCycle,
        stage: PaperCycleStage,
        state: PaperCycleState,
    ) -> Result[PaperCycleStageOutput]:
        if stage is not PaperCycleStage.EXECUTION_RECEIPT:
            return super().advance(command, stage, state)
        self.calls.append(stage)
        if not self.receipt_exists:
            self.execution_side_effects += 1
            self.receipt_exists = True
        if not self.crashed:
            self.crashed = True
            raise SimulatedCrash
        return Success(stage_output(stage))


class FakeCycleStore:
    def __init__(self) -> None:
        self.state = PaperCycleState.genesis(RUN_ID, CYCLE_INPUT.cycle_input_hash)
        self.failures: list[ErrorCode] = []
        self.completed_manifest: ArtifactManifest | None = None

    def load(self, command: RunPaperCycle) -> Result[PaperCycleState]:
        return Success(self.state)

    def checkpoint(
        self,
        command: RunPaperCycle,
        state: PaperCycleState,
        *,
        expected_state_hash: str,
    ) -> Result[PaperCycleState]:
        assert expected_state_hash == self.state.state_hash
        self.state = state
        return Success(state)

    def fail(
        self, command: RunPaperCycle, error: StructuredError
    ) -> Result[PaperCycleRunResult]:
        self.failures.append(error.code)
        return Success(
            PaperCycleRunResult(
                run_id=RUN_ID,
                status=PaperCycleRunStatus.RETRY_SCHEDULED,
                state=self.state,
                result_artifact_hash=None,
                error_code=error.code.value,
            )
        )

    def complete(
        self,
        command: RunPaperCycle,
        state: PaperCycleState,
        artifact: ArtifactManifest,
    ) -> Result[PaperCycleRunResult]:
        self.completed_manifest = artifact
        return Success(
            PaperCycleRunResult(
                run_id=RUN_ID,
                status=PaperCycleRunStatus.SUCCEEDED,
                state=state,
                result_artifact_hash=artifact.content_hash,
                error_code=None,
            )
        )

    def cancel(self, command: CancelPaperCycle) -> Result[PaperCycleRunResult]:
        return Success(
            PaperCycleRunResult(
                run_id=RUN_ID,
                status=PaperCycleRunStatus.CANCELLED,
                state=self.state,
                result_artifact_hash=None,
                error_code=command.reason_code,
            )
        )


def test_runner_checkpoints_exact_canonical_flow_and_final_artifact() -> None:
    store = FakeCycleStore()
    handler = FakeHandler()
    artifacts = MemoryArtifactStore()
    telemetry = RecordingOperationRecorder()

    result = run_paper_fund_cycle(
        request(),
        handler=handler,
        store=store,
        artifacts=artifacts,
        budget=FixedBudgetEvaluator(),
        clock=lambda: NOW,
        telemetry=telemetry,
    )

    assert isinstance(result, Success)
    assert result.value.status is PaperCycleRunStatus.SUCCEEDED
    assert handler.calls == list(PaperCycleStage)
    assert store.state.complete
    assert store.completed_manifest is not None
    assert artifacts.is_finalized(store.completed_manifest.content_hash)
    assert telemetry.calls == [
        (ComponentName.PROVIDER, OperationName.FETCH),
        (ComponentName.MODEL, OperationName.INFER),
        (ComponentName.SIGNAL, OperationName.DERIVE),
        (ComponentName.SIGNAL, OperationName.DERIVE),
        (ComponentName.RISK, OperationName.AUTHORIZE),
        (ComponentName.EXECUTION, OperationName.AUTHORIZE),
        (ComponentName.EXECUTION, OperationName.EXECUTE),
        (ComponentName.EXECUTION, OperationName.COMPLETE),
        (ComponentName.DELIVERY, OperationName.GENERATE),
    ]


def test_runner_schedules_retry_without_advancing_failed_stage() -> None:
    store = FakeCycleStore()
    handler = FakeHandler(fail_at=PaperCycleStage.SIGNAL)

    result = run_paper_fund_cycle(
        request(),
        handler=handler,
        store=store,
        artifacts=MemoryArtifactStore(),
        budget=FixedBudgetEvaluator(),
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert result.value.status is PaperCycleRunStatus.RETRY_SCHEDULED
    assert store.state.completed_stages == (
        PaperCycleStage.EVIDENCE,
        PaperCycleStage.RESEARCH_OPINION,
    )
    assert store.failures == [ErrorCode.DATA_UNAVAILABLE]


def test_execution_crash_retries_from_checkpoint_and_reuses_receipt() -> None:
    store = FakeCycleStore()
    handler = CrashAfterExecutionHandler()
    artifacts = MemoryArtifactStore()

    with pytest.raises(SimulatedCrash):
        run_paper_fund_cycle(
            request(),
            handler=handler,
            store=store,
            artifacts=artifacts,
            budget=FixedBudgetEvaluator(),
            clock=lambda: NOW,
        )

    assert store.state.next_stage is PaperCycleStage.EXECUTION_RECEIPT
    replay = run_paper_fund_cycle(
        request(generation=2),
        handler=handler,
        store=store,
        artifacts=artifacts,
        budget=FixedBudgetEvaluator(),
        clock=lambda: NOW + timedelta(minutes=1),
    )

    assert isinstance(replay, Success)
    assert replay.value.status is PaperCycleRunStatus.SUCCEEDED
    assert handler.execution_side_effects == 1


def test_budget_degradation_stops_before_target_and_order_creation() -> None:
    store = FakeCycleStore()
    handler = FakeHandler()
    budget = FixedBudgetEvaluator(
        (
            BudgetStatus.WITHIN,
            BudgetStatus.WITHIN,
            BudgetStatus.WITHIN,
            BudgetStatus.DEGRADED,
        )
    )

    result = run_paper_fund_cycle(
        request(),
        handler=handler,
        store=store,
        artifacts=MemoryArtifactStore(),
        budget=budget,
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert result.value.error_code == ErrorCode.BUDGET_EXHAUSTED
    assert handler.calls == [
        PaperCycleStage.EVIDENCE,
        PaperCycleStage.RESEARCH_OPINION,
        PaperCycleStage.SIGNAL,
    ]
    assert PaperCycleStage.PORTFOLIO_TARGET not in store.state.completed_stages
    assert PaperCycleStage.ORDER_INTENT not in store.state.completed_stages


def test_budget_evaluator_failure_stops_before_any_stage() -> None:
    class ExplodingBudget:
        def evaluate(self, scope: object, **kwargs: object) -> object:
            del scope, kwargs
            raise RuntimeError("usage backend failed")

    store = FakeCycleStore()
    handler = FakeHandler()
    result = run_paper_fund_cycle(
        request(),
        handler=handler,
        store=store,
        artifacts=MemoryArtifactStore(),
        budget=ExplodingBudget(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert result.value.error_code == ErrorCode.BUDGET_EXHAUSTED
    assert handler.calls == []


def test_cancel_use_case_delegates_to_audited_store() -> None:
    result = cancel_paper_fund_cycle(
        CancelPaperCycle(
            run_id=RUN_ID,
            expected_version=1,
            actor="paper-operator:test",
            reason_code="operator_requested",
        ),
        store=FakeCycleStore(),
    )

    assert isinstance(result, Success)
    assert result.value.status is PaperCycleRunStatus.CANCELLED


def test_run_command_restores_exact_cycle_input_from_lease_payload() -> None:
    command = request()

    assert command.cycle_input.run_id == RUN_ID
    assert command.cycle_input_hash == command.cycle_input.cycle_input_hash
    assert command.lease.payload["cycle_input"] == command.cycle_input.model_dump(
        mode="json"
    )


@pytest.mark.parametrize("tampering", ["hash", "input", "extra"])
def test_run_command_rejects_tampered_or_ambiguous_lease_payload(
    tampering: str,
) -> None:
    original = lease()
    payload = dict(original.payload)
    if tampering == "hash":
        payload["cycle_input_hash"] = "f" * 64
    elif tampering == "input":
        candidate = dict(payload["cycle_input"])  # type: ignore[arg-type]
        candidate["account_id"] = "another-account"
        payload["cycle_input"] = candidate
    else:
        payload["untrusted"] = True
    changed = original.model_copy(update={"payload": payload})

    with pytest.raises(ValueError, match="paper cycle"):
        RunPaperCycle(lease=changed)
