from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.application.monitoring.mark_to_market import mark_to_market
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.monitoring import MarkToMarketCommand

from .helpers import ACCOUNT_ID, INSTRUMENT, NOW, mark, projection


def command(**changes: object) -> MarkToMarketCommand:
    payload: dict[str, object] = {
        "valuation_id": UUID("84000000-0000-4000-8000-000000000001"),
        "account_id": ACCOUNT_ID,
        "base_currency": "USD",
        "as_of": NOW,
        "ledger": projection(),
        "marks": (mark(),),
        "currency_quantum": Decimal("0.01"),
    }
    return MarkToMarketCommand.model_validate(payload | changes)


def test_mark_to_market_values_settled_ledger_with_pit_marks() -> None:
    result = mark_to_market(command())

    assert isinstance(result, Success)
    assert result.value.cash_value == Decimal("9000.00")
    assert result.value.position_value == Decimal("1000.00")
    assert result.value.nav == Decimal("10000.00")
    assert result.value.cumulative_fees == Decimal("0.00")
    assert result.value.positions[0].instrument_id == INSTRUMENT
    assert result.value.valuation_hash == result.value.expected_valuation_hash()


def test_mark_to_market_is_deterministic_apart_from_valuation_identity() -> None:
    first = mark_to_market(command())
    second = mark_to_market(
        command(valuation_id=UUID("84000000-0000-4000-8000-000000000002"))
    )

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert first.value.valuation_hash == second.value.valuation_hash


def test_mark_to_market_fails_closed_for_missing_future_or_foreign_marks() -> None:
    missing = mark_to_market(command(marks=()))
    future = mark_to_market(
        command(marks=(mark(available_at=NOW + timedelta(seconds=1)),))
    )
    foreign = mark_to_market(
        command(marks=(mark(instrument_id=UUID(int=INSTRUMENT.int + 1)),))
    )

    assert all(isinstance(item, Failure) for item in (missing, future, foreign))


def test_mark_to_market_fails_closed_for_future_ledger_or_currency_drift() -> None:
    future_ledger = mark_to_market(
        command(ledger=projection(at=NOW + timedelta(seconds=1)))
    )
    foreign_currency = mark_to_market(
        command(marks=(mark().model_copy(update={"currency": "TWD"}),))
    )

    assert isinstance(future_ledger, Failure)
    assert isinstance(foreign_currency, Failure)
