from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.application.ledger.post import LedgerPostingPolicy
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.orders import OrderSide
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    CashBalance,
    PositionBalance,
)

NOW = datetime(2026, 7, 13, 16, 0, tzinfo=UTC)
ACCOUNT_ID = "paper-ledger"
INSTRUMENT_ID = UUID("46000000-0000-4000-8000-000000000001")
ORDER_ID = UUID("46000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("46000000-0000-4000-8000-000000000003")
FILL_ID = UUID("46000000-0000-4000-8000-000000000004")


def policy() -> LedgerPostingPolicy:
    return LedgerPostingPolicy(
        policy_version="1.0.0",
        cost_basis_method="average",
        monetary_rounding="ROUND_HALF_EVEN",
    )


def opening(*, cash: str = "1000.00") -> AccountPortfolioSnapshot:
    return AccountPortfolioSnapshot(
        snapshot_id=UUID("46000000-0000-4000-8000-000000000005"),
        account_id=ACCOUNT_ID,
        as_of=NOW,
        account_aggregate_sequence=0,
        portfolio_sequence=0,
        ledger_sequence=0,
        ledger_hash=None,
        cash=(
            CashBalance(
                currency="USD",
                settled_amount=Decimal(cash),
                reserved_amount=Decimal("0.00"),
                quantum=Decimal("0.01"),
            ),
        ),
    )


def opening_with_unvalued_position() -> AccountPortfolioSnapshot:
    return opening().model_copy(
        update={
            "positions": (
                PositionBalance(
                    instrument_id=INSTRUMENT_ID,
                    quantity=Decimal("2"),
                    sellable_quantity=Decimal("2"),
                    reserved_quantity=Decimal("0"),
                    quantum=Decimal("1"),
                ),
            )
        }
    )


def fill(
    *,
    side: OrderSide = OrderSide.BUY,
    fill_id: UUID = FILL_ID,
    quantity: str = "2",
    price: str = "100.00",
    fees: str = "1.00",
    occurred_at: datetime = NOW + timedelta(minutes=1),
) -> Fill:
    return Fill(
        fill_id=fill_id,
        command_id=COMMAND_ID,
        order_intent_id=ORDER_ID,
        account_id=ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        side=side,
        quantity=Decimal(quantity),
        quantity_quantum=Decimal("1"),
        price=Decimal(price),
        price_quantum=Decimal("0.01"),
        fee_currency="USD",
        fees=Decimal(fees),
        fee_quantum=Decimal("0.01"),
        slippage=Decimal("0.01"),
        occurred_at=occurred_at,
    )
