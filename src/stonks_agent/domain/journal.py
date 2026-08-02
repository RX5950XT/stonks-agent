"""Balanced, hash-chained double-entry journal domain."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel, is_quantized
from stonks_contracts.common import (
    NonEmptyString,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64


class JournalSide(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class JournalPosting(TradingModel):
    posting_id: UUID
    ledger_account: str = Field(
        pattern=r"^(asset|inventory|fee|pnl|clearing):[A-Za-z0-9_.:-]{1,191}$"
    )
    commodity: NonEmptyString
    side: JournalSide
    amount: PositiveDecimal
    quantum: PositiveDecimal
    memo: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_posting(self) -> Self:
        if not is_quantized(self.amount, self.quantum):
            raise ValueError("posting amount must already match commodity quantum")
        return self

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.side is JournalSide.DEBIT else -self.amount


class JournalTransaction(TradingModel):
    transaction_id: UUID
    account_id: NonEmptyString
    sequence: int = Field(ge=1)
    occurred_at: UTCDateTime
    previous_hash: Sha256 | None = None
    source_order_intent_id: UUID
    source_fill_id: UUID
    postings: tuple[JournalPosting, ...] = Field(min_length=2, max_length=10_000)
    transaction_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        transaction_id: UUID,
        account_id: str,
        sequence: int,
        occurred_at: datetime,
        previous_hash: str | None,
        source_order_intent_id: UUID,
        source_fill_id: UUID,
        postings: tuple[JournalPosting, ...],
    ) -> JournalTransaction:
        values = {
            "transaction_id": transaction_id,
            "account_id": account_id,
            "sequence": sequence,
            "occurred_at": occurred_at,
            "previous_hash": previous_hash,
            "source_order_intent_id": source_order_intent_id,
            "source_fill_id": source_fill_id,
            "postings": postings,
        }
        provisional = cls.model_construct(
            transaction_id=transaction_id,
            account_id=account_id,
            sequence=sequence,
            occurred_at=occurred_at,
            previous_hash=previous_hash,
            source_order_intent_id=source_order_intent_id,
            source_fill_id=source_fill_id,
            postings=postings,
            transaction_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"transaction_hash": provisional.expected_transaction_hash()}
        )

    @model_validator(mode="after")
    def validate_transaction(self) -> Self:
        if (self.sequence == 1) != (self.previous_hash is None):
            raise ValueError("only genesis journal transaction may omit previous hash")
        posting_ids = tuple(str(item.posting_id) for item in self.postings)
        if len(posting_ids) != len(set(posting_ids)):
            raise ValueError("journal posting ids must be unique")
        if posting_ids != tuple(sorted(posting_ids)):
            raise ValueError("journal postings must be stably sorted by id")
        quantums: dict[str, Decimal] = {}
        balances: dict[str, Decimal] = defaultdict(Decimal)
        for posting in self.postings:
            existing = quantums.setdefault(posting.commodity, posting.quantum)
            if existing != posting.quantum:
                raise ValueError("one commodity cannot use mixed quantums")
            balances[posting.commodity] += posting.signed_amount
        unbalanced = sorted(
            commodity for commodity, balance in balances.items() if balance != 0
        )
        if unbalanced:
            raise ValueError(f"unbalanced journal commodities: {', '.join(unbalanced)}")
        if self.transaction_hash != self.expected_transaction_hash():
            raise ValueError("journal transaction hash does not match payload")
        return self

    def is_balanced(self) -> bool:
        balances: dict[str, Decimal] = defaultdict(Decimal)
        for posting in self.postings:
            balances[posting.commodity] += posting.signed_amount
        return bool(self.postings) and all(
            balance == 0 for balance in balances.values()
        )

    def expected_transaction_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"transaction_hash"})
        )


def verify_journal_chain(transactions: tuple[JournalTransaction, ...]) -> bool:
    if not transactions:
        return False
    account_id = transactions[0].account_id
    for index, transaction in enumerate(transactions, start=1):
        expected_previous = (
            transactions[index - 2].transaction_hash if index > 1 else None
        )
        if (
            transaction.account_id != account_id
            or transaction.sequence != index
            or transaction.previous_hash != expected_previous
            or transaction.transaction_hash != transaction.expected_transaction_hash()
            or not transaction.is_balanced()
        ):
            return False
    return True
