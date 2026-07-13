from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

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
from stonks_agent.ports.artifact_store import ArtifactManifest

NOW = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)
RUN_ID = UUID("47000000-0000-4000-8000-000000000101")
JOB_ID = UUID("47000000-0000-4000-8000-000000000102")

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
        payload={"cycle_input_hash": "a" * 64},
        attempt_generation=generation,
        attempt_nonce=f"attempt-{generation}",
        lease_owner="core-runner",
        lease_until=NOW + timedelta(minutes=5),
        attempts=generation,
        deadline_at=NOW + timedelta(hours=1),
    )


def request(*, generation: int = 1) -> RunPaperCycle:
    return RunPaperCycle(lease=lease(generation=generation), cycle_input_hash="a" * 64)


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
        self, stage: PaperCycleStage, state: PaperCycleState
    ) -> Result[PaperCycleStageOutput]:
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
        self, stage: PaperCycleStage, state: PaperCycleState
    ) -> Result[PaperCycleStageOutput]:
        if stage is not PaperCycleStage.EXECUTION_RECEIPT:
            return super().advance(stage, state)
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
        self.state = PaperCycleState.genesis(RUN_ID, "a" * 64)
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

    result = run_paper_fund_cycle(
        request(),
        handler=handler,
        store=store,
        artifacts=artifacts,
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert result.value.status is PaperCycleRunStatus.SUCCEEDED
    assert handler.calls == list(PaperCycleStage)
    assert store.state.complete
    assert store.completed_manifest is not None
    assert artifacts.is_finalized(store.completed_manifest.content_hash)


def test_runner_schedules_retry_without_advancing_failed_stage() -> None:
    store = FakeCycleStore()
    handler = FakeHandler(fail_at=PaperCycleStage.SIGNAL)

    result = run_paper_fund_cycle(
        request(),
        handler=handler,
        store=store,
        artifacts=MemoryArtifactStore(),
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
            clock=lambda: NOW,
        )

    assert store.state.next_stage is PaperCycleStage.EXECUTION_RECEIPT
    replay = run_paper_fund_cycle(
        request(generation=2),
        handler=handler,
        store=store,
        artifacts=artifacts,
        clock=lambda: NOW + timedelta(minutes=1),
    )

    assert isinstance(replay, Success)
    assert replay.value.status is PaperCycleRunStatus.SUCCEEDED
    assert handler.execution_side_effects == 1


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
