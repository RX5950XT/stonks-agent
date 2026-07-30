from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.domain.gui_paper import (
    GuiPaperCashView,
    GuiPaperIntegrityView,
    GuiPaperPortfolioView,
    GuiPaperRiskView,
    GuiPaperSafetyView,
)
from stonks_agent.domain.gui_research import (
    GuiResearchEvidenceField,
    GuiResearchEvidenceItem,
    GuiResearchEvidenceView,
    GuiResearchHistoryItem,
    GuiResearchHistoryView,
    GuiResearchIssueView,
    GuiResearchUsageView,
)

NOW = datetime(2026, 7, 29, 8, tzinfo=UTC)
RUN_ID = UUID("8b000000-0000-4000-8000-000000000001")
EVIDENCE_ID = UUID("8b000000-0000-4000-8000-000000000002")


def test_research_history_is_bounded_and_keeps_terminal_transparency() -> None:
    item = GuiResearchHistoryItem(
        run_id=RUN_ID,
        symbol="AAPL",
        profile="balanced/1",
        status="degraded",
        stage="report",
        as_of=NOW,
        confidence=Decimal("0.71"),
        issue_count=1,
        updated_at=NOW,
    )

    view = GuiResearchHistoryView(items=(item,))

    assert view.items[0].issue_count == 1
    with pytest.raises(ValidationError):
        GuiResearchHistoryView(items=(item,) * 21)


def test_evidence_projection_exposes_only_bounded_text_fields() -> None:
    view = GuiResearchEvidenceView(
        run_id=RUN_ID,
        items=(
            GuiResearchEvidenceItem(
                evidence_id=EVIDENCE_ID,
                kind="market_data",
                source="openbb:yfinance",
                provider="yfinance",
                event_time=NOW,
                available_at=NOW,
                quality_status="available",
                completeness=Decimal("1"),
                content_hash="a" * 64,
                fields=(GuiResearchEvidenceField(name="close", value="214.05"),),
            ),
        ),
    )

    payload = view.model_dump(mode="json")
    assert payload["items"][0]["fields"] == [{"name": "close", "value": "214.05"}]
    assert "raw_artifact_ref" not in str(payload)
    with pytest.raises(ValidationError):
        GuiResearchEvidenceField(name="close", value="x" * 513)


def test_usage_and_issue_views_are_typed_and_fail_closed() -> None:
    usage = GuiResearchUsageView(
        iterations=2,
        tool_calls=3,
        input_tokens=100,
        output_tokens=20,
        cost_usd=Decimal("0.012"),
        elapsed_ms=250,
    )
    issue = GuiResearchIssueView(
        stage="agent",
        code="tradingagents_unavailable",
    )

    assert usage.total_tokens == 120
    assert issue.code == "tradingagents_unavailable"
    with pytest.raises(ValidationError):
        GuiResearchUsageView(
            iterations=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("-0.01"),
            elapsed_ms=0,
        )


def test_paper_projection_separates_empty_risk_from_verified_portfolio() -> None:
    portfolio = GuiPaperPortfolioView(
        base_currency="USD",
        as_of=NOW,
        cash=(
            GuiPaperCashView(
                currency="USD",
                settled="100000.00",
                reserved="0.00",
                available="100000.00",
            ),
        ),
        positions=(),
        position_count=0,
        pending_order_count=0,
        latest_target=False,
    )
    risk = GuiPaperRiskView(state="empty")
    integrity = GuiPaperIntegrityView(
        state="verified",
        account_sequence=0,
        portfolio_sequence=0,
        ledger_sequence=0,
        projection_hash="b" * 64,
    )
    safety = GuiPaperSafetyView(
        state="available",
        active=False,
        reason_code="bootstrap_inactive",
        version=1,
        updated_at=NOW,
    )

    assert portfolio.cash[0].available == Decimal("100000.00")
    assert risk.approved is None
    assert integrity.state == "verified"
    assert safety.active is False
