from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.domain.portfolio import CashBalance, PositionBalance
from stonks_agent.domain.projections import (
    PortfolioProjection,
    ProjectedCashBalance,
    ProjectedPositionBalance,
    RiskProjection,
)
from stonks_agent.domain.risk import RiskCheck, RiskDecision
from stonks_contracts.report import ReportReference

NOW = datetime(2026, 7, 14, 3, tzinfo=UTC)
INSTRUMENT = UUID("74000000-0000-4000-8000-000000000001")
HASH_A = "a" * 64
HASH_B = "b" * 64


def _target_and_decision():  # type: ignore[no-untyped-def]
    from application.monitoring.helpers import decision, target

    return target(), decision()


def test_portfolio_projection_exposes_exact_reserved_and_available_balances() -> None:
    cash = ProjectedCashBalance.from_balance(
        CashBalance(
            currency="USD",
            settled_amount=Decimal("1000.00"),
            reserved_amount=Decimal("125.00"),
            quantum=Decimal("0.01"),
        )
    )
    position = ProjectedPositionBalance.from_balance(
        PositionBalance(
            instrument_id=INSTRUMENT,
            quantity=Decimal("10"),
            sellable_quantity=Decimal("8"),
            reserved_quantity=Decimal("3"),
            quantum=Decimal("1"),
        )
    )

    projection = PortfolioProjection.create(
        account_id="paper-projection",
        base_currency="USD",
        as_of=NOW,
        account_aggregate_sequence=2,
        portfolio_sequence=1,
        ledger_sequence=1,
        ledger_hash=HASH_A,
        cash=(cash,),
        positions=(position,),
        pending_order_ids=(UUID(int=9),),
        latest_target_ref=ReportReference(ref_id=UUID(int=8), content_hash=HASH_B),
    )

    assert projection.cash[0].available_amount == Decimal("875.00")
    assert projection.positions[0].available_quantity == Decimal("5")
    assert projection.projection_hash == projection.expected_projection_hash()
    assert PortfolioProjection.model_validate(projection.model_dump()) == projection


def test_portfolio_projection_rejects_tampered_derived_values_and_hash() -> None:
    with pytest.raises(ValidationError, match="available cash"):
        ProjectedCashBalance(
            currency="USD",
            settled_amount=Decimal("100.00"),
            reserved_amount=Decimal("10.00"),
            available_amount=Decimal("100.00"),
            quantum=Decimal("0.01"),
        )


def test_risk_projection_reports_latest_decision_without_granting_authority() -> None:
    target, decision = _target_and_decision()
    projection = RiskProjection.create(
        decision=decision,
        observed_account_sequence=decision.account_aggregate_sequence,
        observed_portfolio_sequence=decision.portfolio_sequence,
        as_of=decision.decided_at + timedelta(seconds=1),
    )

    assert projection.decision_id == decision.decision_id
    assert projection.portfolio_target_ref == ReportReference(
        ref_id=target.target_id,
        content_hash=target.calculation_hash,
    )
    assert projection.currently_authorized
    assert not hasattr(projection, "order")

    stale = RiskProjection.create(
        decision=decision,
        observed_account_sequence=decision.account_aggregate_sequence + 1,
        observed_portfolio_sequence=decision.portfolio_sequence,
        as_of=decision.decided_at + timedelta(seconds=1),
    )
    assert not stale.currently_authorized


def test_risk_projection_preserves_sorted_failed_check_reasons() -> None:
    target, approved = _target_and_decision()
    rejected = RiskDecision.create(
        decision_id=UUID("74000000-0000-4000-8000-000000000002"),
        target=target,
        approved=False,
        normalized_target=None,
        checks=(
            RiskCheck(code="cash", passed=False, reason="insufficient cash"),
            RiskCheck(code="session", passed=True),
        ),
        policy_version=approved.policy_version,
        policy_hash=approved.policy_hash,
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )

    projection = RiskProjection.create(
        decision=rejected,
        observed_account_sequence=rejected.account_aggregate_sequence,
        observed_portfolio_sequence=rejected.portfolio_sequence,
        as_of=NOW,
    )

    assert projection.failed_checks == ("cash: insufficient cash",)
    assert not projection.currently_authorized
