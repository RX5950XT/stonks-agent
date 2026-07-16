from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_contracts.report import AnalysisReport, ReportReference

HASH_A = "a" * 64
HASH_B = "b" * 64
TARGET_ID = UUID("73000000-0000-4000-8000-000000000001")
RISK_ID = UUID("73000000-0000-4000-8000-000000000002")


def _report(**overrides: object) -> AnalysisReport:
    values: dict[str, object] = {
        "report_id": UUID("73000000-0000-4000-8000-000000000003"),
        "run_id": UUID("73000000-0000-4000-8000-000000000004"),
        "owner_subject": "paper-report-owner",
        "subject": "paper-account",
        "as_of": datetime(2026, 7, 14, tzinfo=UTC),
        "language": "zh-TW",
        "report_type": "paper_outcome",
        "conclusion": "neutral_outlook",
        "score": Decimal("0.5"),
        "confidence": Decimal("0.5"),
        "action_guardrails": ("paper_only",),
        "evidence_refs": (),
        "portfolio_target_refs": (ReportReference(ref_id=TARGET_ID, content_hash=HASH_A),),
        "risk_decision_refs": (ReportReference(ref_id=RISK_ID, content_hash=HASH_B),),
        "generator_version": "test/1.0.0",
        "policy_version": "test/1.0.0",
    }
    values.update(overrides)
    return AnalysisReport.model_validate(values)


def test_analysis_report_preserves_canonical_trading_references() -> None:
    report = _report()

    assert report.portfolio_target_refs[0].ref_id == TARGET_ID
    assert report.risk_decision_refs[0].content_hash == HASH_B
    assert report.order_intent_refs == report.fill_refs == report.outcome_refs == ()


def test_report_reference_groups_must_be_unique_and_stably_sorted() -> None:
    first = ReportReference(ref_id=TARGET_ID, content_hash=HASH_A)
    second = ReportReference(ref_id=RISK_ID, content_hash=HASH_B)

    with pytest.raises(ValidationError, match="unique and stably sorted"):
        _report(portfolio_target_refs=(second, first))
    with pytest.raises(ValidationError, match="unique and stably sorted"):
        _report(portfolio_target_refs=(first, first))
