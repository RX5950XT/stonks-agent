"""Deterministically rebuild settled account state from immutable journals."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from stonks_agent.domain._trading import failure
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.journal import JournalSide, JournalTransaction
from stonks_agent.domain.ledger import LedgerAccountBalance, LedgerProjection
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot

ZERO = Decimal(0)
type BalanceKey = tuple[str, str]
type MutableTotals = dict[BalanceKey, list[Decimal]]


def replay_journal(
    opening: AccountPortfolioSnapshot,
    transactions: tuple[JournalTransaction, ...],
) -> Result[LedgerProjection]:
    if opening.ledger_sequence != 0 or opening.ledger_hash is not None:
        return failure(ErrorCode.INVALID_INPUT, "Ledger opening must be genesis")
    totals = _opening_totals(opening)
    previous_hash: str | None = None
    last_occurred_at = opening.as_of
    for expected_sequence, transaction in enumerate(transactions, start=1):
        invalid = _transaction_error(
            transaction,
            account_id=opening.account_id,
            expected_sequence=expected_sequence,
            previous_hash=previous_hash,
            last_occurred_at=last_occurred_at,
        )
        if invalid is not None:
            return invalid
        for posting in transaction.postings:
            if not _recognized(posting.ledger_account, posting.commodity):
                return failure(ErrorCode.CONFLICT, "Unknown canonical ledger account")
            key = (posting.ledger_account, posting.commodity)
            current = totals.setdefault(key, [ZERO, ZERO, posting.quantum])
            if current[2] != posting.quantum:
                return failure(ErrorCode.CONFLICT, "Ledger commodity quantum changed")
            index = 0 if posting.side is JournalSide.DEBIT else 1
            current[index] += posting.amount
        projection = _projection(
            opening,
            totals,
            ledger_sequence=transaction.sequence,
            ledger_hash=transaction.transaction_hash,
            last_occurred_at=transaction.occurred_at,
        )
        if _has_negative_asset(projection):
            return failure(ErrorCode.CONFLICT, "Ledger replay produced negative assets")
        previous_hash = transaction.transaction_hash
        last_occurred_at = transaction.occurred_at
    return Success(
        _projection(
            opening,
            totals,
            ledger_sequence=len(transactions),
            ledger_hash=previous_hash,
            last_occurred_at=last_occurred_at,
        )
    )


def _opening_totals(opening: AccountPortfolioSnapshot) -> MutableTotals:
    totals: MutableTotals = {}
    for cash in opening.cash:
        totals[(f"asset:cash:{cash.currency}", cash.currency)] = [
            cash.settled_amount,
            ZERO,
            cash.quantum,
        ]
    for position in opening.positions:
        commodity = str(position.instrument_id)
        totals[(f"inventory:units:{commodity}", commodity)] = [
            position.quantity,
            ZERO,
            position.quantum,
        ]
    return totals


def _transaction_error(
    transaction: JournalTransaction,
    *,
    account_id: str,
    expected_sequence: int,
    previous_hash: str | None,
    last_occurred_at: datetime,
) -> Failure | None:
    if (
        transaction.account_id != account_id
        or transaction.sequence != expected_sequence
        or transaction.previous_hash != previous_hash
        or transaction.occurred_at < last_occurred_at
        or transaction.transaction_hash != transaction.expected_transaction_hash()
        or not transaction.is_balanced()
    ):
        return failure(ErrorCode.CONFLICT, "Ledger journal chain is invalid")
    return None


def _recognized(account: str, commodity: str) -> bool:
    parts = account.split(":")
    if len(parts) == 3 and parts[:2] in (
        ["asset", "cash"],
        ["fee", "execution"],
        ["pnl", "realized"],
        ["clearing", "cash"],
    ):
        return parts[2] == commodity and commodity.isupper() and len(commodity) == 3
    if len(parts) == 3 and parts[:2] in (
        ["inventory", "units"],
        ["clearing", "units"],
    ):
        return _uuid_matches(parts[2], commodity)
    if len(parts) == 4 and parts[:2] == ["inventory", "value"]:
        return (
            _uuid_matches(parts[2], parts[2])
            and parts[3] == commodity
            and commodity.isupper()
            and len(commodity) == 3
        )
    return False


def _uuid_matches(value: str, commodity: str) -> bool:
    try:
        return str(UUID(value)) == value and value == commodity
    except ValueError:
        return False


def _projection(
    opening: AccountPortfolioSnapshot,
    totals: MutableTotals,
    *,
    ledger_sequence: int,
    ledger_hash: str | None,
    last_occurred_at: datetime,
) -> LedgerProjection:
    balances = tuple(
        LedgerAccountBalance(
            ledger_account=account,
            commodity=commodity,
            debit_total=values[0],
            credit_total=values[1],
            quantum=values[2],
        )
        for (account, commodity), values in sorted(totals.items())
    )
    unvalued = tuple(
        sorted(
            (item.instrument_id for item in opening.positions if item.quantity > 0),
            key=str,
        )
    )
    return LedgerProjection.create(
        account_id=opening.account_id,
        opening_snapshot_hash=opening.snapshot_hash,
        ledger_sequence=ledger_sequence,
        ledger_hash=ledger_hash,
        last_occurred_at=last_occurred_at,
        balances=balances,
        unvalued_instrument_ids=unvalued,
    )


def _has_negative_asset(projection: LedgerProjection) -> bool:
    for item in projection.balances:
        if (
            item.ledger_account.startswith(("asset:cash:", "inventory:"))
            and item.balance < 0
        ):
            return True
    return False
