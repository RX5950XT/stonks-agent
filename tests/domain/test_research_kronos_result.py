from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.domain.errors import ErrorCode
from stonks_agent.domain.research_job import KronosResearchOutcome
from stonks_agent.domain.signal import SignalEligibilityDecision

RUN_ID = UUID("92400000-0000-4000-8000-000000000001")
SNAPSHOT_ID = UUID("92400000-0000-4000-8000-000000000002")


def test_failed_kronos_outcome_is_typed_and_has_zero_authority() -> None:
    outcome = KronosResearchOutcome.failed(
        run_id=RUN_ID,
        snapshot_id=SNAPSHOT_ID,
        error_code=ErrorCode.DATA_UNAVAILABLE,
    )

    assert outcome.status == "failed"
    assert outcome.forecast_output is None
    assert outcome.alpha_signal is None
    assert outcome.actual_model_inference is False
    assert outcome.eligibility == SignalEligibilityDecision(
        eligible=False,
        weight=Decimal(0),
        reason_codes=("forecast_unavailable", "data_unavailable"),
    )


def test_failed_kronos_outcome_rejects_nonzero_paper_weight() -> None:
    with pytest.raises(ValidationError):
        KronosResearchOutcome(
            run_id=RUN_ID,
            snapshot_id=SNAPSHOT_ID,
            status="failed",
            actual_model_inference=False,
            error_code=ErrorCode.DATA_UNAVAILABLE,
            alpha_status="blocked",
            eligibility=SignalEligibilityDecision(
                eligible=True,
                weight=Decimal("0.5"),
                reason_codes=("eligible",),
            ),
        )
