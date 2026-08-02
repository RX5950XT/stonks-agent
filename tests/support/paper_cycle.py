from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from stonks_agent.domain.paper_cycle import (
    PaperCyclePolicyHashes,
    PaperCycleStage,
    PaperCycleStageIdentity,
    PaperFundCycleInput,
)

NOW = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)


def paper_cycle_input(
    *,
    run_id: UUID,
    as_of: datetime = NOW,
    deadline_at: datetime | None = None,
) -> PaperFundCycleInput:
    return PaperFundCycleInput(
        run_id=run_id,
        snapshot_id=UUID("71000000-0000-4000-8000-000000000001"),
        research_run_id=UUID("71000000-0000-4000-8000-000000000002"),
        research_artifact_id=UUID("71000000-0000-4000-8000-000000000003"),
        account_id="paper-local",
        owner_subject="local:paper-cycle",
        instrument_id=UUID("71000000-0000-4000-8000-000000000004"),
        symbol="AAPL",
        as_of=as_of,
        created_at=as_of + timedelta(seconds=1),
        deadline_at=deadline_at or as_of + timedelta(hours=1),
        execution_mode="paper",
        execution_model_version="paper-v1",
        policy_hashes=PaperCyclePolicyHashes(
            research_profile_hash="1" * 64,
            model_policy_hash="2" * 64,
            tool_policy_hash="3" * 64,
            kronos_configuration_hash="4" * 64,
            portfolio_policy_hash="5" * 64,
            risk_policy_hash="6" * 64,
            execution_policy_hash="7" * 64,
            ledger_policy_hash="8" * 64,
            report_policy_hash="9" * 64,
        ),
        stage_ids=tuple(
            PaperCycleStageIdentity(
                stage=stage,
                stage_id=UUID(f"72000000-0000-4000-8000-{position:012d}"),
            )
            for position, stage in enumerate(PaperCycleStage, start=1)
        ),
    )


def paper_cycle_payload(value: PaperFundCycleInput) -> dict[str, object]:
    return {
        "cycle_input": value.model_dump(mode="json"),
        "cycle_input_hash": value.cycle_input_hash,
    }
