from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from stonks_contracts.common import Money
from stonks_contracts.execution import (
    JournalPosting,
    JournalSide,
    OrderIntent,
    OrderSide,
    OrderType,
    TimeInForce,
)
from stonks_contracts.portfolio import PortfolioTarget, TargetAllocation

NOW = datetime(2026, 7, 10, 8, 30, tzinfo=UTC)
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000001")
SIGNAL_ID = UUID("00000000-0000-4000-8000-000000000002")
SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000003")
TARGET_ID = UUID("00000000-0000-4000-8000-000000000004")
RISK_ID = UUID("00000000-0000-4000-8000-000000000005")
RESERVATION_ID = UUID("00000000-0000-4000-8000-000000000006")
INTENT_ID = UUID("00000000-0000-4000-8000-000000000007")


@pytest.fixture
def portfolio_target() -> PortfolioTarget:
    return PortfolioTarget(
        target_id=TARGET_ID,
        account_id="paper-main",
        portfolio_snapshot_id=SNAPSHOT_ID,
        as_of=NOW,
        allocations=(
            TargetAllocation(
                instrument_id=INSTRUMENT_ID,
                target_weight=Decimal("0.25"),
                current_quantity=Decimal("0"),
                target_quantity=Decimal("10"),
                delta_quantity=Decimal("10"),
            ),
        ),
        input_signal_ids=(SIGNAL_ID,),
        policy_version="equal-weight/1.0.0",
        expected_turnover=Decimal("0.25"),
        expected_cost=Money(currency="USD", amount=Decimal("1.25")),
        calculation_hash="a" * 64,
    )


@pytest.fixture
def order_intent() -> OrderIntent:
    return OrderIntent(
        intent_id=INTENT_ID,
        run_id=UUID("00000000-0000-4000-8000-000000000008"),
        account_id="paper-main",
        instrument_id=INSTRUMENT_ID,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        time_in_force=TimeInForce.DAY,
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=8),
        risk_decision_id=RISK_ID,
        reservation_id=RESERVATION_ID,
        portfolio_snapshot_id=SNAPSHOT_ID,
        aggregate_sequence=7,
        idempotency_key="run-8:AAPL:buy",
        execution_model_version="next-bar/1.0.0",
        created_at=NOW,
    )


@pytest.fixture
def balanced_postings() -> tuple[JournalPosting, ...]:
    return (
        JournalPosting(
            account="assets:cash:paper-main",
            commodity="USD",
            side=JournalSide.CREDIT,
            amount=Decimal("101.25"),
        ),
        JournalPosting(
            account="clearing:cash",
            commodity="USD",
            side=JournalSide.DEBIT,
            amount=Decimal("101.25"),
        ),
        JournalPosting(
            account="assets:inventory:paper-main:AAPL",
            commodity=f"instrument:{INSTRUMENT_ID}",
            side=JournalSide.DEBIT,
            amount=Decimal("10"),
        ),
        JournalPosting(
            account="clearing:inventory:AAPL",
            commodity=f"instrument:{INSTRUMENT_ID}",
            side=JournalSide.CREDIT,
            amount=Decimal("10"),
        ),
    )
