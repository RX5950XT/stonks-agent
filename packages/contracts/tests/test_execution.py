from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_contracts.execution import (
    ExecutionCommand,
    JournalPosting,
    JournalSide,
    JournalTransaction,
    OrderIntent,
    OrderStatus,
    OrderType,
)

NOW = datetime(2026, 7, 10, 8, 30, tzinfo=UTC)


def test_journal_balances_each_commodity(
    balanced_postings: tuple[JournalPosting, ...],
) -> None:
    transaction = JournalTransaction(
        transaction_id=UUID("00000000-0000-4000-8000-000000000020"),
        sequence=9,
        occurred_at=NOW,
        previous_hash="b" * 64,
        source_fill_id=UUID("00000000-0000-4000-8000-000000000021"),
        postings=balanced_postings,
    )

    assert len(transaction.postings) == 4
    assert transaction.model_dump(mode="json")["postings"][0]["amount"] == "101.25"


def test_journal_rejects_unbalanced_commodity(
    balanced_postings: tuple[JournalPosting, ...],
) -> None:
    broken = (
        *balanced_postings[:-1],
        JournalPosting(
            account="clearing:inventory:AAPL",
            commodity=balanced_postings[-1].commodity,
            side=JournalSide.CREDIT,
            amount=Decimal("9"),
        ),
    )

    with pytest.raises(ValidationError, match=r"unbalanced.*instrument"):
        JournalTransaction(
            transaction_id=UUID("00000000-0000-4000-8000-000000000020"),
            sequence=9,
            occurred_at=NOW,
            previous_hash="b" * 64,
            source_fill_id=UUID("00000000-0000-4000-8000-000000000021"),
            postings=broken,
        )


def test_journal_rejects_less_than_two_postings() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        JournalTransaction(
            transaction_id=UUID("00000000-0000-4000-8000-000000000020"),
            sequence=9,
            occurred_at=NOW,
            source_fill_id=UUID("00000000-0000-4000-8000-000000000021"),
            postings=(
                JournalPosting(
                    account="assets:cash",
                    commodity="USD",
                    side=JournalSide.DEBIT,
                    amount=Decimal("1"),
                ),
            ),
        )


def test_limit_order_requires_limit_price(order_intent: OrderIntent) -> None:
    invalid = order_intent.model_dump(mode="python") | {
        "order_type": OrderType.LIMIT,
        "limit_price": None,
    }

    with pytest.raises(ValidationError, match="limit_price"):
        OrderIntent.model_validate(invalid)


def test_order_validity_window_must_increase(order_intent: OrderIntent) -> None:
    invalid = order_intent.model_dump(mode="python") | {
        "valid_until": order_intent.valid_from - timedelta(seconds=1)
    }

    with pytest.raises(ValidationError, match="valid_until"):
        OrderIntent.model_validate(invalid)


def test_execution_command_carries_fencing_tokens(order_intent: OrderIntent) -> None:
    command = ExecutionCommand(
        command_id=UUID("00000000-0000-4000-8000-000000000022"),
        intent=order_intent,
        attempt_generation=3,
        attempt_nonce="nonce-3",
        issued_at=NOW,
    )

    assert command.intent.intent_id == order_intent.intent_id
    assert command.attempt_generation == 3


def test_receipt_quantity_cannot_exceed_command_quantity(order_intent: OrderIntent) -> None:
    from stonks_contracts.execution import ExecutionReceipt

    with pytest.raises(ValidationError, match=r"filled_quantity.*quantity"):
        ExecutionReceipt(
            receipt_id=UUID("00000000-0000-4000-8000-000000000023"),
            command_id=UUID("00000000-0000-4000-8000-000000000022"),
            order_intent_id=order_intent.intent_id,
            status=OrderStatus.FILLED,
            occurred_at=NOW,
            sequence=1,
            filled_quantity=Decimal("11"),
            remaining_quantity=Decimal("0"),
            command_quantity=order_intent.quantity,
        )


def test_receipt_rejects_unknown_execution_state(order_intent: OrderIntent) -> None:
    from stonks_contracts.execution import ExecutionReceipt

    with pytest.raises(ValidationError, match="status"):
        ExecutionReceipt.model_validate(
            {
                "receipt_id": "00000000-0000-4000-8000-000000000023",
                "command_id": "00000000-0000-4000-8000-000000000022",
                "order_intent_id": str(order_intent.intent_id),
                "status": "unknown",
                "occurred_at": NOW,
                "sequence": 1,
                "filled_quantity": "0",
                "remaining_quantity": str(order_intent.quantity),
                "command_quantity": str(order_intent.quantity),
            }
        )
