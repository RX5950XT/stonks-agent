from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_contracts import CANONICAL_CHAIN
from stonks_contracts.common import ContractModel
from stonks_contracts.research import AgentOpinion, AnalysisBundle, ResearchArtifact
from stonks_contracts.signal import PromotionState

FORBIDDEN_AUTHORITY_TOKENS = ("order", "qty", "quantity", "execution")


@pytest.mark.parametrize("model", [ResearchArtifact, AnalysisBundle, AgentOpinion])
def test_research_outputs_have_no_execution_authority(model: type[ContractModel]) -> None:
    field_names = model.model_fields

    assert not any(
        token in name.lower() for name in field_names for token in FORBIDDEN_AUTHORITY_TOKENS
    )


def test_agent_opinion_rejects_an_order_field() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentOpinion.model_validate(
            {
                "opinion_id": UUID("00000000-0000-4000-8000-000000000010"),
                "instrument_id": UUID("00000000-0000-4000-8000-000000000001"),
                "as_of": "2026-07-10T08:30:00Z",
                "horizon": "5d",
                "recommendation": "bullish",
                "thesis": "Positive earnings revision breadth.",
                "confidence": "0.65",
                "calibration": "uncalibrated",
                "producer": "deterministic-test",
                "model_version": "rules/1.0.0",
                "order": "buy",
            }
        )


def test_canonical_chain_has_no_trade_intent() -> None:
    assert "TradeIntent" not in CANONICAL_CHAIN
    assert CANONICAL_CHAIN == (
        "EvidenceItem/ResearchArtifact",
        "AnalysisBundle/AgentOpinion/AlphaSignal/ForecastSignal",
        "PortfolioTarget",
        "RiskDecision",
        "AccountReservation",
        "OrderIntent",
        "ExecutionReceipt/Fill",
        "JournalTransaction",
        "AnalysisReport",
    )


def test_wire_promotion_state_includes_suspension_but_never_live() -> None:
    assert PromotionState.SUSPENDED.value == "suspended"
    assert "live" not in {state.value for state in PromotionState}
