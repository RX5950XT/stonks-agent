"""Append-only canonical journal boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.ledger import LedgerHead, LedgerProjection
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot
from stonks_agent.domain.trading_persistence import PaperExecutionRecord


@runtime_checkable
class LedgerPort(Protocol):
    def get_head(self, account_id: str) -> Result[LedgerHead]: ...

    def get_opening_snapshot(
        self, account_id: str
    ) -> Result[AccountPortfolioSnapshot]: ...

    def get_projection(self, account_id: str) -> Result[LedgerProjection]: ...

    def append(
        self,
        transaction: JournalTransaction,
        *,
        expected_sequence: int,
        expected_hash: str | None,
        expected_account_sequence: int,
    ) -> Result[JournalTransaction]: ...

    def list_transactions(
        self,
        account_id: str,
        *,
        after_sequence: int = 0,
    ) -> Result[tuple[JournalTransaction, ...]]: ...

    def validate_execution_graph(
        self, record: PaperExecutionRecord
    ) -> Result[bool]: ...

    def validate_account_graph(self, account_id: str) -> Result[bool]: ...

    def execution_enabled(self, account_id: str) -> Result[bool]: ...

    def activate_global_kill_switch(
        self, *, reason_code: str, actor: str
    ) -> Result[bool]: ...
