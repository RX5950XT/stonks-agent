from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.domain.ledger import LedgerAccountBalance, LedgerProjection
from stonks_agent.domain.monitoring import (
    PointInTimeMark,
    PortfolioValuation,
    PositionValuation,
)
from stonks_agent.domain.portfolio import PortfolioTarget, TargetAllocation
from stonks_agent.domain.risk import RiskCheck, RiskDecision

NOW = datetime(2026, 7, 1, 14, tzinfo=UTC)
ACCOUNT_ID = "paper-monitoring"
INSTRUMENT = UUID("81000000-0000-4000-8000-000000000001")
BENCHMARK = UUID("81000000-0000-4000-8000-000000000002")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def projection(
    *,
    at: datetime = NOW,
    cash: Decimal = Decimal("9000.00"),
    fees: Decimal = Decimal("0.00"),
    sequence: int = 1,
) -> LedgerProjection:
    balances = tuple(
        sorted(
            (
                LedgerAccountBalance(
                    ledger_account="asset:cash:USD",
                    commodity="USD",
                    quantum=Decimal("0.01"),
                    debit_total=cash,
                    credit_total=Decimal("0.00"),
                ),
                LedgerAccountBalance(
                    ledger_account="fee:execution:USD",
                    commodity="USD",
                    quantum=Decimal("0.01"),
                    debit_total=fees,
                    credit_total=Decimal("0.00"),
                ),
                LedgerAccountBalance(
                    ledger_account=f"inventory:units:{INSTRUMENT}",
                    commodity=str(INSTRUMENT),
                    quantum=Decimal("1"),
                    debit_total=Decimal("10"),
                    credit_total=Decimal("0"),
                ),
            ),
            key=lambda item: (item.ledger_account, item.commodity),
        )
    )
    return LedgerProjection.create(
        account_id=ACCOUNT_ID,
        opening_snapshot_hash=HASH_A,
        ledger_sequence=sequence,
        ledger_hash=HASH_B,
        last_occurred_at=at,
        balances=balances,
        unvalued_instrument_ids=(INSTRUMENT,),
    )


def mark(
    *,
    instrument_id: UUID = INSTRUMENT,
    price: Decimal = Decimal("100"),
    at: datetime = NOW,
    available_at: datetime | None = None,
    evidence_id: UUID | None = None,
) -> PointInTimeMark:
    return PointInTimeMark(
        instrument_id=instrument_id,
        currency="USD",
        price=price,
        event_time=at - timedelta(minutes=1),
        available_at=available_at or at,
        evidence_id=evidence_id or UUID(int=instrument_id.int + 100),
        source_artifact_ref=f"sha256:{HASH_C}",
    )


def target() -> PortfolioTarget:
    return PortfolioTarget.create(
        target_id=UUID("82000000-0000-4000-8000-000000000001"),
        account_id=ACCOUNT_ID,
        portfolio_snapshot_id=UUID("82000000-0000-4000-8000-000000000002"),
        account_aggregate_sequence=1,
        portfolio_sequence=1,
        as_of=NOW - timedelta(minutes=5),
        allocations=(
            TargetAllocation(
                instrument_id=INSTRUMENT,
                current_quantity=Decimal("0"),
                target_quantity=Decimal("10"),
                delta_quantity=Decimal("10"),
                quantity_quantum=Decimal("1"),
                target_weight=Decimal("0.1"),
            ),
        ),
        input_signal_ids=(UUID("82000000-0000-4000-8000-000000000003"),),
        policy_version="1.0.0",
        policy_hash=HASH_A,
        expected_turnover=Decimal("0.1"),
        expected_cost=Decimal("2.00"),
        cost_currency="USD",
    )


def decision() -> RiskDecision:
    value = target()
    return RiskDecision.create(
        decision_id=UUID("83000000-0000-4000-8000-000000000001"),
        target=value,
        approved=True,
        normalized_target=value,
        checks=(RiskCheck(code="cash", passed=True),),
        policy_version="1.0.0",
        policy_hash=HASH_B,
        decided_at=NOW - timedelta(minutes=4),
        expires_at=NOW + timedelta(minutes=30),
    )


def valuation(
    *,
    identifier: int,
    at: datetime,
    nav: Decimal,
    fees: Decimal,
    ledger_sequence: int,
) -> PortfolioValuation:
    valued_position = PositionValuation(
        instrument_id=INSTRUMENT,
        quantity=Decimal("10"),
        mark=mark(price=Decimal("100"), at=at),
        market_value=Decimal("1000.00"),
        currency_quantum=Decimal("0.01"),
    )
    return PortfolioValuation.create(
        valuation_id=UUID(int=identifier),
        account_id=ACCOUNT_ID,
        base_currency="USD",
        as_of=at,
        ledger_sequence=ledger_sequence,
        ledger_hash=HASH_B,
        ledger_projection_hash=HASH_C,
        currency_quantum=Decimal("0.01"),
        cash_value=nav - Decimal("1000"),
        position_value=Decimal("1000"),
        nav=nav,
        cumulative_fees=fees,
        realized_pnl=Decimal("0"),
        positions=(valued_position,),
    )
