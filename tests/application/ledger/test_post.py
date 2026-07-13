from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from stonks_agent.application.ledger.post import build_fill_journal
from stonks_agent.application.ledger.replay import replay_journal
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.orders import OrderSide

from .helpers import (
    FILL_ID,
    INSTRUMENT_ID,
    fill,
    opening,
    opening_with_unvalued_position,
    policy,
)


def test_buy_posts_cash_inventory_fee_and_clearing_then_replays() -> None:
    initial = replay_journal(opening(), ())
    assert isinstance(initial, Success)

    built = build_fill_journal(fill(), initial.value, policy())

    assert isinstance(built, Success)
    transaction = built.value
    assert transaction.is_balanced()
    assert {item.ledger_account.split(":", 1)[0] for item in transaction.postings} == {
        "asset",
        "clearing",
        "fee",
        "inventory",
    }
    replayed = replay_journal(opening(), (transaction,))
    assert isinstance(replayed, Success)
    assert replayed.value.cash("USD") == Decimal("799.00")
    assert replayed.value.position(INSTRUMENT_ID) == Decimal("2")
    assert replayed.value.inventory_value(INSTRUMENT_ID, "USD") == Decimal("200.00")
    assert replayed.value.fees("USD") == Decimal("1.00")
    assert replayed.value.realized_pnl("USD") == Decimal("0.00")


def test_average_cost_sell_posts_realized_profit_and_preserves_balance() -> None:
    initial = replay_journal(opening(), ())
    assert isinstance(initial, Success)
    bought = build_fill_journal(
        fill(quantity="4", price="50.00"), initial.value, policy()
    )
    assert isinstance(bought, Success)
    after_buy = replay_journal(opening(), (bought.value,))
    assert isinstance(after_buy, Success)
    sold_fill = fill(
        side=OrderSide.SELL,
        fill_id=UUID("46000000-0000-4000-8000-000000000006"),
        quantity="2",
        price="75.00",
        fees="1.00",
    )

    sold = build_fill_journal(sold_fill, after_buy.value, policy())

    assert isinstance(sold, Success)
    replayed = replay_journal(opening(), (bought.value, sold.value))
    assert isinstance(replayed, Success)
    assert replayed.value.cash("USD") == Decimal("948.00")
    assert replayed.value.position(INSTRUMENT_ID) == Decimal("2")
    assert replayed.value.inventory_value(INSTRUMENT_ID, "USD") == Decimal("100.00")
    assert replayed.value.fees("USD") == Decimal("2.00")
    assert replayed.value.realized_pnl("USD") == Decimal("50.00")


def test_sell_with_unknown_opening_cost_basis_fails_closed() -> None:
    initial = replay_journal(opening_with_unvalued_position(), ())
    assert isinstance(initial, Success)

    result = build_fill_journal(fill(side=OrderSide.SELL), initial.value, policy())

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


@given(
    quantity=st.integers(min_value=1, max_value=100),
    cents=st.integers(min_value=1, max_value=100_000),
    fee_cents=st.integers(min_value=0, max_value=10_000),
)
def test_buy_postings_are_balanced_after_decimal_quantization(
    quantity: int, cents: int, fee_cents: int
) -> None:
    initial = replay_journal(opening(cash="999999999.99"), ())
    assert isinstance(initial, Success)
    candidate = fill(
        fill_id=UUID(int=FILL_ID.int + quantity + cents + fee_cents),
        quantity=str(quantity),
        price=str(Decimal(cents) / 100),
        fees=str(Decimal(fee_cents) / 100),
    )

    result = build_fill_journal(candidate, initial.value, policy())

    assert isinstance(result, Success)
    assert result.value.is_balanced()
