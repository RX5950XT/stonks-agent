from __future__ import annotations

from uuid import UUID

import pytest

from stonks_agent.domain.paper_cycle import (
    CanonicalCycleReference,
    PaperCycleStage,
    PaperCycleStageOutput,
    PaperCycleState,
)

RUN_ID = UUID("47000000-0000-4000-8000-000000000001")


def reference(ref_type: str, suffix: int) -> CanonicalCycleReference:
    return CanonicalCycleReference(
        ref_type=ref_type,
        ref_id=f"47000000-0000-4000-8000-{suffix:012d}",
        content_hash=f"{suffix:064x}",
    )


def output(
    stage: PaperCycleStage,
    ref_type: str,
    suffix: int,
) -> PaperCycleStageOutput:
    return PaperCycleStageOutput.create(
        stage=stage,
        references=(reference(ref_type, suffix),),
    )


def test_state_accepts_only_the_canonical_stage_prefix_and_hashes_it() -> None:
    state = PaperCycleState.genesis(RUN_ID, "a" * 64)
    evidence = output(PaperCycleStage.EVIDENCE, "evidence", 2)

    advanced = state.advance(evidence)

    assert advanced.completed_stages == (PaperCycleStage.EVIDENCE,)
    assert advanced.outputs == (evidence,)
    assert advanced.state_hash == advanced.expected_state_hash()
    assert advanced.next_stage is PaperCycleStage.RESEARCH_OPINION


def test_state_rejects_skipped_stage_wrong_reference_type_and_tampering() -> None:
    state = PaperCycleState.genesis(RUN_ID, "a" * 64)
    skipped = output(PaperCycleStage.SIGNAL, "alpha_signal", 3)

    with pytest.raises(ValueError, match="next canonical stage"):
        state.advance(skipped)
    with pytest.raises(ValueError, match="reference types"):
        PaperCycleStageOutput.create(
            stage=PaperCycleStage.EVIDENCE,
            references=(reference("order_intent", 4),),
        )
    with pytest.raises(ValueError, match="state hash"):
        state.model_copy(update={"state_hash": "f" * 64}).model_validate(
            state.model_copy(update={"state_hash": "f" * 64})
        )


def test_rejected_risk_path_keeps_order_and_execution_stages_explicitly_empty() -> None:
    values = (
        output(PaperCycleStage.EVIDENCE, "evidence", 10),
        output(
            PaperCycleStage.RESEARCH_OPINION,
            "research_artifact",
            11,
        ),
        output(PaperCycleStage.SIGNAL, "alpha_signal", 12),
        output(PaperCycleStage.PORTFOLIO_TARGET, "portfolio_target", 13),
        output(PaperCycleStage.RISK_DECISION, "risk_decision", 14),
        PaperCycleStageOutput.create(
            stage=PaperCycleStage.ORDER_INTENT,
            references=(),
        ),
        PaperCycleStageOutput.create(
            stage=PaperCycleStage.EXECUTION_RECEIPT,
            references=(),
        ),
        output(PaperCycleStage.LEDGER, "ledger_projection", 15),
        output(PaperCycleStage.REPORT, "analysis_report", 16),
    )
    state = PaperCycleState.genesis(RUN_ID, "a" * 64)

    for item in values:
        state = state.advance(item)

    assert state.complete
    assert state.next_stage is None


def test_execution_reference_count_must_match_order_count() -> None:
    state = PaperCycleState.genesis(RUN_ID, "a" * 64)
    prefix = (
        output(PaperCycleStage.EVIDENCE, "evidence", 20),
        output(
            PaperCycleStage.RESEARCH_OPINION,
            "research_artifact",
            21,
        ),
        output(PaperCycleStage.SIGNAL, "alpha_signal", 22),
        output(PaperCycleStage.PORTFOLIO_TARGET, "portfolio_target", 23),
        output(PaperCycleStage.RISK_DECISION, "risk_decision", 24),
        output(PaperCycleStage.ORDER_INTENT, "order_intent", 25),
    )
    for item in prefix:
        state = state.advance(item)

    empty_execution = PaperCycleStageOutput.create(
        stage=PaperCycleStage.EXECUTION_RECEIPT,
        references=(),
    )
    with pytest.raises(ValueError, match="receipt count"):
        state.advance(empty_execution)
