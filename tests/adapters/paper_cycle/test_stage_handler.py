from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from support.paper_cycle import paper_cycle_input, paper_cycle_payload

from stonks_agent.adapters.artifacts.local import LocalArtifactStore
from stonks_agent.adapters.paper_cycle.stage_handler import (
    ArtifactBackedPaperCycleStageHandler,
    PaperCycleStageValue,
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
    PaperCycleStage,
    PaperCycleState,
    RunPaperCycle,
)

NOW = datetime(2026, 7, 28, 6, tzinfo=UTC)
RUN_ID = UUID("73400000-0000-4000-8000-000000000001")

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


class StageObject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object_id: UUID
    stage: PaperCycleStage


def _command() -> RunPaperCycle:
    cycle_input = paper_cycle_input(
        run_id=RUN_ID,
        as_of=NOW,
        deadline_at=NOW + timedelta(hours=1),
    )
    return RunPaperCycle(
        lease=JobLease(
            job_id=UUID("73400000-0000-4000-8000-000000000002"),
            run_id=RUN_ID,
            job_type="paper_fund_cycle",
            payload=paper_cycle_payload(cycle_input),
            attempt_generation=1,
            attempt_nonce="paper-cycle-nonce",
            lease_owner="core-runner",
            lease_until=NOW + timedelta(minutes=5),
            attempts=1,
            deadline_at=cycle_input.deadline_at,
        ),
        cycle_input=cycle_input,
    )


def _processor(stage: PaperCycleStage):
    def process(
        _command: RunPaperCycle,
        _state: PaperCycleState,
    ) -> Result[tuple[PaperCycleStageValue, ...]]:
        object_id = UUID(
            f"73500000-0000-4000-8000-{tuple(PaperCycleStage).index(stage) + 1:012d}"
        )
        return Success(
            (
                PaperCycleStageValue(
                    ref_type=REFERENCE_TYPES[stage],
                    ref_id=str(object_id),
                    value=StageObject(object_id=object_id, stage=stage),
                ),
            )
        )

    return process


def test_handler_runs_exact_stage_bindings_and_finalizes_typed_artifacts(
    tmp_path: Path,
) -> None:
    command = _command()
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    handler = ArtifactBackedPaperCycleStageHandler(
        processors={stage: _processor(stage) for stage in PaperCycleStage},
        artifacts=artifacts,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    state = PaperCycleState.genesis(command.lease.run_id, command.cycle_input_hash)

    for stage in PaperCycleStage:
        advanced = handler.advance(command, stage, state)
        assert isinstance(advanced, Success)
        state = state.advance(advanced.value)
        reference = advanced.value.references[0]
        stored = artifacts.read(reference.content_hash)
        assert isinstance(stored, Success)
        assert StageObject.model_validate_json(stored.value).stage is stage

    assert state.complete
    assert state.completed_stages == tuple(PaperCycleStage)


def test_handler_requires_exact_nine_stage_composition(tmp_path: Path) -> None:
    processors = {stage: _processor(stage) for stage in PaperCycleStage}
    processors.pop(PaperCycleStage.SIGNAL)

    with pytest.raises(ValueError, match="exact canonical stage set"):
        ArtifactBackedPaperCycleStageHandler(
            processors=processors,
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
            clock=lambda: NOW,
        )


def test_handler_stops_at_typed_signal_failure_without_later_side_effects(
    tmp_path: Path,
) -> None:
    calls: list[PaperCycleStage] = []

    def processor(stage: PaperCycleStage):
        base = _processor(stage)

        def process(
            command: RunPaperCycle,
            state: PaperCycleState,
        ) -> Result[tuple[PaperCycleStageValue, ...]]:
            calls.append(stage)
            if stage is PaperCycleStage.SIGNAL:
                return Failure(
                    StructuredError(
                        code=ErrorCode.CAPABILITY_DENIED,
                        message="No paper-eligible signal exists",
                    )
                )
            return base(command, state)

        return process

    command = _command()
    handler = ArtifactBackedPaperCycleStageHandler(
        processors={stage: processor(stage) for stage in PaperCycleStage},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        clock=lambda: NOW + timedelta(seconds=3),
    )
    state = PaperCycleState.genesis(command.lease.run_id, command.cycle_input_hash)
    for stage in (
        PaperCycleStage.EVIDENCE,
        PaperCycleStage.RESEARCH_OPINION,
    ):
        advanced = handler.advance(command, stage, state)
        assert isinstance(advanced, Success)
        state = state.advance(advanced.value)

    failed = handler.advance(command, PaperCycleStage.SIGNAL, state)

    assert isinstance(failed, Failure)
    assert failed.error.code is ErrorCode.CAPABILITY_DENIED
    assert calls == [
        PaperCycleStage.EVIDENCE,
        PaperCycleStage.RESEARCH_OPINION,
        PaperCycleStage.SIGNAL,
    ]


def test_handler_rejects_out_of_order_or_foreign_state(tmp_path: Path) -> None:
    command = _command()
    handler = ArtifactBackedPaperCycleStageHandler(
        processors={stage: _processor(stage) for stage in PaperCycleStage},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        clock=lambda: NOW,
    )
    state = PaperCycleState.genesis(command.lease.run_id, command.cycle_input_hash)

    out_of_order = handler.advance(command, PaperCycleStage.SIGNAL, state)
    foreign = handler.advance(
        command,
        PaperCycleStage.EVIDENCE,
        state.model_copy(update={"cycle_input_hash": "f" * 64}),
    )

    assert isinstance(out_of_order, Failure)
    assert out_of_order.error.code is ErrorCode.CONFLICT
    assert isinstance(foreign, Failure)
    assert foreign.error.code is ErrorCode.CONFLICT
