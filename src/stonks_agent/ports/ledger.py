"""Append-only canonical journal boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel
from stonks_agent.domain.errors import Result
from stonks_agent.domain.journal import JournalTransaction
from stonks_contracts.common import NonEmptyString, Sha256


class LedgerHead(TradingModel):
    account_id: NonEmptyString
    sequence: int = Field(ge=0)
    transaction_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_head(self) -> LedgerHead:
        if (self.sequence == 0) != (self.transaction_hash is None):
            raise ValueError("only genesis ledger head may omit transaction hash")
        return self


@runtime_checkable
class LedgerPort(Protocol):
    def get_head(self, account_id: str) -> Result[LedgerHead]: ...

    def append(
        self,
        transaction: JournalTransaction,
        *,
        expected_sequence: int,
        expected_hash: str | None,
    ) -> Result[JournalTransaction]: ...

    def list_transactions(
        self,
        account_id: str,
        *,
        after_sequence: int = 0,
    ) -> Result[tuple[JournalTransaction, ...]]: ...
