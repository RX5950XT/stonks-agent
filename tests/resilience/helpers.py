from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.paper_cycle import (
    CanonicalCycleReference,
    PaperCycleRunResult,
    PaperCycleRunStatus,
    PaperCycleStage,
    PaperCycleStageOutput,
    PaperCycleState,
    RunPaperCycle,
)
from stonks_agent.ports.artifact_store import ArtifactManifest

NOW = datetime(2026, 7, 22, 1, 0, tzinfo=UTC)
RUN_ID = UUID("69000000-0000-4000-8000-000000000001")
JOB_ID = UUID("69000000-0000-4000-8000-000000000002")
INPUT_HASH = "a" * 64
MAX_ATTEMPTS = 3

_REFERENCE_TYPES = {
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


def cycle_command(*, generation: int = 1) -> RunPaperCycle:
    lease = JobLease(
        job_id=JOB_ID,
        run_id=RUN_ID,
        job_type="paper_fund_cycle",
        payload={"cycle_input_hash": INPUT_HASH},
        attempt_generation=generation,
        attempt_nonce=f"fault-attempt-{generation}",
        lease_owner="core-runner",
        lease_until=NOW + timedelta(minutes=5),
        attempts=generation,
        deadline_at=NOW + timedelta(hours=1),
    )
    return RunPaperCycle(lease=lease, cycle_input_hash=INPUT_HASH)


def stage_output(stage: PaperCycleStage) -> PaperCycleStageOutput:
    position = tuple(PaperCycleStage).index(stage) + 1
    return PaperCycleStageOutput.create(
        stage=stage,
        references=(
            CanonicalCycleReference(
                ref_type=_REFERENCE_TYPES[stage],
                ref_id=f"resilience-ref-{position}",
                content_hash=f"{position:064x}",
            ),
        ),
    )


class FaultingStageHandler:
    def __init__(
        self,
        *,
        fail_at: PaperCycleStage | None = None,
        error_code: ErrorCode = ErrorCode.DATA_UNAVAILABLE,
    ) -> None:
        self.fail_at = fail_at
        self.error_code = error_code
        self.calls: list[PaperCycleStage] = []

    def advance(
        self,
        stage: PaperCycleStage,
        state: PaperCycleState,
    ) -> Result[PaperCycleStageOutput]:
        del state
        self.calls.append(stage)
        if stage is self.fail_at:
            return Failure(
                StructuredError(
                    code=self.error_code,
                    message="injected upstream outage",
                )
            )
        return Success(stage_output(stage))


class ResilientCycleStore:
    def __init__(self) -> None:
        self.state = PaperCycleState.genesis(RUN_ID, INPUT_HASH)
        self.failures: list[ErrorCode] = []
        self.completed_manifest: ArtifactManifest | None = None

    def load(self, command: RunPaperCycle) -> Result[PaperCycleState]:
        del command
        return Success(self.state)

    def checkpoint(
        self,
        command: RunPaperCycle,
        state: PaperCycleState,
        *,
        expected_state_hash: str,
    ) -> Result[PaperCycleState]:
        del command
        if expected_state_hash != self.state.state_hash:
            return _failure(ErrorCode.CONFLICT, "checkpoint compare-and-swap failed")
        self.state = state
        return Success(state)

    def fail(
        self,
        command: RunPaperCycle,
        error: StructuredError,
    ) -> Result[PaperCycleRunResult]:
        self.failures.append(error.code)
        terminal = command.lease.attempts >= MAX_ATTEMPTS
        return Success(
            PaperCycleRunResult(
                run_id=RUN_ID,
                status=(
                    PaperCycleRunStatus.DEAD_LETTERED
                    if terminal
                    else PaperCycleRunStatus.RETRY_SCHEDULED
                ),
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
        del command
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

    def cancel(self, command: object) -> Result[PaperCycleRunResult]:
        del command
        return _failure(ErrorCode.INVALID_STATE, "not used by resilience tests")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
