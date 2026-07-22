from __future__ import annotations

import pytest
from support.budgets import FixedBudgetEvaluator

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.workflows.run_cycle import run_paper_fund_cycle
from stonks_agent.domain.errors import ErrorCode, Success
from stonks_agent.domain.paper_cycle import PaperCycleRunStatus, PaperCycleStage

from .helpers import (
    MAX_ATTEMPTS,
    NOW,
    FaultingStageHandler,
    ResilientCycleStore,
    cycle_command,
)


@pytest.mark.parametrize(
    ("fault", "stage", "code"),
    (
        ("provider", PaperCycleStage.EVIDENCE, ErrorCode.DATA_UNAVAILABLE),
        ("llm", PaperCycleStage.RESEARCH_OPINION, ErrorCode.DATA_UNAVAILABLE),
        ("model", PaperCycleStage.SIGNAL, ErrorCode.DATA_UNAVAILABLE),
        ("sidecar", PaperCycleStage.RESEARCH_OPINION, ErrorCode.DEADLINE_EXCEEDED),
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_upstream_outage_stops_before_target_and_recovers_from_checkpoint(
    fault: str,
    stage: PaperCycleStage,
    code: ErrorCode,
) -> None:
    del fault
    store = ResilientCycleStore()
    failed_handler = FaultingStageHandler(fail_at=stage, error_code=code)
    artifacts = MemoryArtifactStore()

    failed = run_paper_fund_cycle(
        cycle_command(),
        handler=failed_handler,
        store=store,
        artifacts=artifacts,
        budget=FixedBudgetEvaluator(),
        clock=lambda: NOW,
    )

    assert isinstance(failed, Success)
    assert failed.value.status is PaperCycleRunStatus.RETRY_SCHEDULED
    assert failed.value.error_code == code.value
    assert PaperCycleStage.PORTFOLIO_TARGET not in store.state.completed_stages
    assert PaperCycleStage.ORDER_INTENT not in store.state.completed_stages
    assert PaperCycleStage.PORTFOLIO_TARGET not in failed_handler.calls
    assert PaperCycleStage.ORDER_INTENT not in failed_handler.calls

    recovered_handler = FaultingStageHandler()
    recovered = run_paper_fund_cycle(
        cycle_command(generation=2),
        handler=recovered_handler,
        store=store,
        artifacts=artifacts,
        budget=FixedBudgetEvaluator(),
        clock=lambda: NOW,
    )

    assert isinstance(recovered, Success)
    assert recovered.value.status is PaperCycleRunStatus.SUCCEEDED
    assert recovered_handler.calls[0] is stage
    assert recovered_handler.calls.count(PaperCycleStage.PORTFOLIO_TARGET) == 1
    assert recovered_handler.calls.count(PaperCycleStage.ORDER_INTENT) == 1
    assert store.completed_manifest is not None
    assert artifacts.is_finalized(store.completed_manifest.content_hash)


def test_attempt_exhaustion_dead_letters_without_target_or_order() -> None:
    store = ResilientCycleStore()
    handler = FaultingStageHandler(fail_at=PaperCycleStage.EVIDENCE)

    result = run_paper_fund_cycle(
        cycle_command(generation=MAX_ATTEMPTS),
        handler=handler,
        store=store,
        artifacts=MemoryArtifactStore(),
        budget=FixedBudgetEvaluator(),
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert result.value.status is PaperCycleRunStatus.DEAD_LETTERED
    assert store.state.completed_stages == ()
    assert handler.calls == [PaperCycleStage.EVIDENCE]
