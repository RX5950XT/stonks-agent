from __future__ import annotations

from decimal import Decimal

from stonks_agent.application.ledger.post import build_fill_journal
from stonks_agent.application.ledger.reconcile import compare_ledger_projection
from stonks_agent.application.ledger.replay import replay_journal
from stonks_agent.domain.errors import ErrorCode, Failure, Success

from .helpers import ACCOUNT_ID, fill, opening, policy


def test_replay_is_deterministic_and_rejects_gap_or_tampered_hash() -> None:
    initial = replay_journal(opening(), ())
    assert isinstance(initial, Success)
    posted = build_fill_journal(fill(), initial.value, policy())
    assert isinstance(posted, Success)

    first = replay_journal(opening(), (posted.value,))
    second = replay_journal(opening(), (posted.value,))
    gap = posted.value.model_copy(update={"sequence": 2})
    tampered = posted.value.model_copy(update={"transaction_hash": "f" * 64})

    assert isinstance(first, Success) and isinstance(second, Success)
    assert first.value == second.value
    assert first.value.projection_hash == second.value.projection_hash
    assert isinstance(replay_journal(opening(), (gap,)), Failure)
    corrupt = replay_journal(opening(), (tampered,))
    assert isinstance(corrupt, Failure)
    assert corrupt.error.code is ErrorCode.CONFLICT


def test_replay_rejects_negative_cash_and_unknown_ledger_account() -> None:
    initial = replay_journal(opening(cash="1.00"), ())
    assert isinstance(initial, Success)
    expensive = build_fill_journal(fill(price="100.00"), initial.value, policy())
    assert isinstance(expensive, Success)
    negative = replay_journal(opening(cash="1.00"), (expensive.value,))
    unknown_posting = expensive.value.postings[0].model_copy(
        update={"ledger_account": "pnl:unknown:USD"}
    )
    postings = tuple(
        sorted(
            (unknown_posting, *expensive.value.postings[1:]),
            key=lambda item: str(item.posting_id),
        )
    )
    unknown = expensive.value.model_copy(update={"postings": postings})

    assert isinstance(negative, Failure)
    assert negative.error.code is ErrorCode.CONFLICT
    invalid_account = replay_journal(opening(), (unknown,))
    assert isinstance(invalid_account, Failure)
    assert invalid_account.error.code is ErrorCode.CONFLICT


def test_opening_projection_is_ledger_genesis() -> None:
    result = replay_journal(opening(), ())

    assert isinstance(result, Success)
    assert result.value.account_id == ACCOUNT_ID
    assert result.value.ledger_sequence == 0
    assert result.value.ledger_hash is None
    assert result.value.cash("USD") == Decimal("1000.00")


def test_reconciliation_hash_matches_replay_and_detects_projection_drift() -> None:
    initial = replay_journal(opening(), ())
    assert isinstance(initial, Success)
    posted = build_fill_journal(fill(), initial.value, policy())
    assert isinstance(posted, Success)
    replayed = replay_journal(opening(), (posted.value,))
    assert isinstance(replayed, Success)
    exact = compare_ledger_projection(
        opening(), (posted.value,), replayed.value, as_of=posted.value.occurred_at
    )
    cash = replayed.value.balances[0].model_copy(
        update={"debit_total": replayed.value.balances[0].debit_total + Decimal("0.01")}
    )
    drifted = replayed.value.create(
        account_id=replayed.value.account_id,
        opening_snapshot_hash=replayed.value.opening_snapshot_hash,
        ledger_sequence=replayed.value.ledger_sequence,
        ledger_hash=replayed.value.ledger_hash,
        last_occurred_at=replayed.value.last_occurred_at,
        balances=(cash, *replayed.value.balances[1:]),
        unvalued_instrument_ids=replayed.value.unvalued_instrument_ids,
    )
    mismatch = compare_ledger_projection(
        opening(), (posted.value,), drifted, as_of=posted.value.occurred_at
    )

    assert isinstance(exact, Success) and exact.value.matched
    assert isinstance(mismatch, Success) and not mismatch.value.matched
    assert mismatch.value.mismatch_reasons == ("projection_hash_mismatch",)
