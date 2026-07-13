"""Append-only canonical journal boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.ledger import LedgerHead


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
